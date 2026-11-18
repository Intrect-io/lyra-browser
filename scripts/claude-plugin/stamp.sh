# Sourced by bootstrap.sh and dev-install.sh: the digest a built environment is stamped with.
# Rebuilt when the lock, the project metadata or the plugin's install path changes (the path
# changes with every plugin version, and the venv was installed from that path).
# sha256sum is GNU coreutils; macOS ships shasum instead (and no sha256sum unless
# coreutils is installed). Both print the same digest.
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum
  else
    shasum -a 256
  fi
}

# want_stamp <root>: the stamp bootstrap.sh expects for an environment built from <root>.
want_stamp() {
  local root="$1"
  {
    cat "$root/pyproject.toml"
    [[ -f "$root/uv.lock" ]] && cat "$root/uv.lock"
    echo "root=$root"
  } | sha256 | cut -d' ' -f1
}
