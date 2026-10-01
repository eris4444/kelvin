PREFIX ?= /usr
DESTDIR ?=

LIBDIR := $(DESTDIR)$(PREFIX)/lib/gpu-s
SHAREDIR := $(DESTDIR)$(PREFIX)/share

.PHONY: install test

install:
	install -Dm755 bin/gpu-s $(DESTDIR)$(PREFIX)/bin/gpu-s
	install -d $(LIBDIR)/gpu_s/ui
	install -m644 gpu_s/*.py $(LIBDIR)/gpu_s/
	install -m644 gpu_s/ui/*.py gpu_s/ui/style.css $(LIBDIR)/gpu_s/ui/
	install -m755 helpers/gpu-s-pm-helper helpers/gpu-s-mux-helper helpers/gpu-s-battery-helper $(LIBDIR)/
	install -Dm644 data/dev.erisrtg.gpus.policy $(SHAREDIR)/polkit-1/actions/dev.erisrtg.gpus.policy
	install -Dm644 data/dev.erisrtg.GpuS.desktop $(SHAREDIR)/applications/dev.erisrtg.GpuS.desktop
	install -Dm644 data/icons/hicolor/scalable/apps/dev.erisrtg.GpuS.svg \
		$(SHAREDIR)/icons/hicolor/scalable/apps/dev.erisrtg.GpuS.svg
	install -d $(SHAREDIR)/gpu-s/icons/hicolor/scalable/apps $(SHAREDIR)/gpu-s/icons/hicolor/symbolic/apps
	install -m644 data/icons/hicolor/scalable/apps/*.svg $(SHAREDIR)/gpu-s/icons/hicolor/scalable/apps/
	install -m644 data/icons/hicolor/symbolic/apps/*.svg $(SHAREDIR)/gpu-s/icons/hicolor/symbolic/apps/
	install -Dm644 README.md $(SHAREDIR)/doc/gpu-s/README.md
	install -Dm644 LICENSE $(SHAREDIR)/licenses/gpu-s/LICENSE
	python3 -m compileall -q -s "$(DESTDIR)" $(LIBDIR)/gpu_s

test:
	python3 -m unittest discover -s tests
	bash tests/test_helpers.sh
