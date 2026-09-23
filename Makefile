UV_CACHE_DIR ?= .uv-cache
export UV_CACHE_DIR
LIMIT ?= 5

.PHONY: all extract extract-all refresh-listings verify serve

all: extract

extract:
	uv run python extract.py --limit $(LIMIT)

extract-all:
	uv run python extract.py --all

refresh-listings:
	uv run python extract.py --refresh-listings --limit $(LIMIT)

verify:
	uv run python extract.py --verify-only

serve:
	python3 -m http.server 8765
