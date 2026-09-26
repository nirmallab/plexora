// No console window behind the app in a release build. `--smoke-test` still
// reports through inherited pipes, which is how release.py reads it.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    plexora_desktop_lib::run()
}
