PREFIX ?= /usr
DESTDIR ?=

LIBDIR := $(DESTDIR)$(PREFIX)/lib/kelvin
SHAREDIR := $(DESTDIR)$(PREFIX)/share

.PHONY: install test

install:
	install -Dm755 bin/kelvin $(DESTDIR)$(PREFIX)/bin/kelvin
	ln -sf kelvin $(DESTDIR)$(PREFIX)/bin/gpu-s  # compatibility: Kelvin was called GPU-S
	install -d $(LIBDIR)/kelvin/ui
	install -m644 kelvin/*.py $(LIBDIR)/kelvin/
	install -m644 kelvin/ui/*.py kelvin/ui/style.css $(LIBDIR)/kelvin/ui/
	install -m755 helpers/kelvin-pm-helper helpers/kelvin-mux-helper helpers/kelvin-battery-helper helpers/kelvin-fan-helper $(LIBDIR)/
	install -Dm644 data/dev.erisrtg.kelvin.policy $(SHAREDIR)/polkit-1/actions/dev.erisrtg.kelvin.policy
	install -Dm644 data/dev.erisrtg.Kelvin.desktop $(SHAREDIR)/applications/dev.erisrtg.Kelvin.desktop
	install -Dm644 data/icons/hicolor/scalable/apps/dev.erisrtg.Kelvin.svg \
		$(SHAREDIR)/icons/hicolor/scalable/apps/dev.erisrtg.Kelvin.svg
	install -d $(SHAREDIR)/kelvin/icons/hicolor/scalable/apps $(SHAREDIR)/kelvin/icons/hicolor/symbolic/apps
	install -m644 data/icons/hicolor/scalable/apps/*.svg $(SHAREDIR)/kelvin/icons/hicolor/scalable/apps/
	install -m644 data/icons/hicolor/symbolic/apps/*.svg $(SHAREDIR)/kelvin/icons/hicolor/symbolic/apps/
	install -Dm644 data/omarchy-plugin/kelvin.idle/manifest.json $(SHAREDIR)/kelvin/omarchy-plugin/kelvin.idle/manifest.json
	install -Dm644 data/omarchy-plugin/kelvin.idle/Service.qml $(SHAREDIR)/kelvin/omarchy-plugin/kelvin.idle/Service.qml
	install -Dm644 README.md $(SHAREDIR)/doc/kelvin/README.md
	install -Dm644 LICENSE $(SHAREDIR)/licenses/kelvin/LICENSE
	python3 -m compileall -q -s "$(DESTDIR)" $(LIBDIR)/kelvin

test:
	python3 -m unittest discover -s tests
	bash tests/test_helpers.sh
