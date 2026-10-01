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

pm=helpers/gpu-s-pm-helper
mux=helpers/gpu-s-mux-helper

expect 2 "pm: no args" bash "$pm"
expect 2 "pm: bad mode" bash "$pm" off
expect 2 "pm: injection attempt" bash "$pm" 'auto;id'
expect 2 "pm: bad address" bash "$pm" on ../../etc
expect 2 "pm: too many args" bash "$pm" on 0000:01:00.0 extra
expect 1 "pm: refuses to run unprivileged" bash "$pm" on
expect 2 "mux: no args" bash "$mux"
expect 2 "mux: bad target" bash "$mux" integrated

bat=helpers/gpu-s-battery-helper
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

if [[ -e /sys/class/firmware-attributes/asus-armoury/attributes/gpu_mux_mode ]]; then
  other=$([[ $(</sys/class/firmware-attributes/asus-armoury/attributes/gpu_mux_mode/current_value) == 1 ]] && echo discrete || echo hybrid)
  expect 1 "mux: real switch refused unprivileged" bash "$mux" "$other"
  expect 0 "mux: check-hybrid is read-only and allowed" bash "$mux" check-hybrid
  expect 0 "mux: check-discrete is read-only and allowed" bash "$mux" check-discrete
fi

echo "helper tests: $pass passed, $fail failed"
((fail == 0))
