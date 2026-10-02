#!/bin/bash
# Argument-validation tests for the root helpers. Runs unprivileged: every
# case must be rejected (or be a read-only check) before anything is written.
set -u
cd "$(dirname "$0")/.." || exit 1
pass=0 fail=0

expect() {
  local want=$1 desc=$2
  shift 2
  "$@" >/dev/null 2>&1
  local got=$?
  if [[ $got == "$want" ]]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $desc (exit $got, expected $want)"
  fi
}

pm=helpers/kelvin-pm-helper
mux=helpers/kelvin-mux-helper

expect 2 "pm: no args" bash "$pm"
expect 2 "pm: bad mode" bash "$pm" off
expect 2 "pm: injection attempt" bash "$pm" 'auto;id'
expect 2 "pm: bad address" bash "$pm" on ../../etc
expect 2 "pm: too many args" bash "$pm" on 0000:01:00.0 extra
expect 1 "pm: refuses to run unprivileged" bash "$pm" on
expect 2 "mux: no args" bash "$mux"
expect 2 "mux: bad target" bash "$mux" integrated

bat=helpers/kelvin-battery-helper
expect 2 "battery: no args" bash "$bat"
expect 2 "battery: bad battery name" bash "$bat" set ../../etc 50 80
expect 2 "battery: bad action" bash "$bat" write BAT1
expect 2 "battery: end too low" bash "$bat" set BAT1 _ 20
expect 2 "battery: end too high" bash "$bat" set BAT1 _ 101
expect 2 "battery: non-numeric end" bash "$bat" set BAT1 _ '80;id'
expect 2 "battery: missing end" bash "$bat" set BAT1 _
if [[ -e /sys/class/power_supply/BAT1/charge_control_end_threshold ]]; then
  if [[ ! -e /sys/class/power_supply/BAT1/charge_control_start_threshold ]]; then
    expect 4 "battery: start threshold refused when unsupported" bash "$bat" set BAT1 75 80
  fi
  expect 1 "battery: valid set refused unprivileged" bash "$bat" set BAT1 _ 80
fi

fan=helpers/kelvin-fan-helper
safe=50:0,58:38,64:64,70:102,75:140,80:179,85:217,90:255
expect 2 "fan: no args" bash "$fan"
expect 2 "fan: bad fan" bash "$fan" set cpu1 "$safe"
expect 2 "fan: bad action" bash "$fan" write cpu
expect 2 "fan: seven points" bash "$fan" set cpu 50:0,58:38,64:64,70:102,75:140,80:179,85:217
expect 2 "fan: injection" bash "$fan" set cpu '50:0;id'
expect 2 "fan: temps not increasing" bash "$fan" set cpu 50:0,50:38,64:64,70:102,75:140,80:179,85:217,90:255
expect 2 "fan: speed decreasing" bash "$fan" set cpu 50:40,58:38,64:64,70:102,75:140,80:179,85:217,90:255
expect 7 "fan: too slow at 70C" bash "$fan" set cpu 50:0,58:20,64:30,70:40,75:140,80:179,85:217,90:255
expect 7 "fan: too slow at 80C" bash "$fan" set cpu 50:0,58:38,64:64,70:102,75:110,80:120,85:217,90:255
expect 7 "fan: last point too hot" bash "$fan" set cpu 50:0,58:38,64:64,70:102,75:140,80:179,90:217,99:255
expect 1 "fan: safe curve refused unprivileged" bash "$fan" set cpu "$safe"
expect 1 "fan: auto refused unprivileged" bash "$fan" auto gpu

if [[ -e /sys/class/firmware-attributes/asus-armoury/attributes/gpu_mux_mode ]]; then
  other=$([[ $(</sys/class/firmware-attributes/asus-armoury/attributes/gpu_mux_mode/current_value) == 1 ]] && echo discrete || echo hybrid)
  expect 1 "mux: real switch refused unprivileged" bash "$mux" "$other"
  expect 0 "mux: check-hybrid is read-only and allowed" bash "$mux" check-hybrid
  expect 0 "mux: check-discrete is read-only and allowed" bash "$mux" check-discrete
fi

echo "helper tests: $pass passed, $fail failed"
((fail == 0))
