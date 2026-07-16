# cc-loop — Rust delivery engine

.PHONY: build test clippy release install rust-build rust-test rust-clippy rust-release install-rust-bin

build: rust-build
test: rust-test
clippy: rust-clippy
release: rust-release
install: install-rust-bin

rust-build:
	cd rust && cargo build

rust-release:
	cd rust && cargo build --release

rust-test:
	cd rust && cargo test --workspace

rust-clippy:
	cd rust && cargo clippy -p cc-loop-core -p cc-loop-cli -- -D warnings

install-rust-bin: rust-release
	install -m 755 rust/target/release/cc-loop "$(HOME)/.local/bin/cc-loop"
	@echo "Installed $(HOME)/.local/bin/cc-loop (ensure ~/.local/bin is on PATH)"
