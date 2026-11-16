#!/bin/bash
set -euo pipefail

ROOT=/tmp/vm-panel-root
rm -rf "$ROOT"
mkdir -p "$ROOT/opt/app/templates" "$ROOT/opt/app/data" "$ROOT/tmp" "$ROOT/proc" "$ROOT/dev" "$ROOT/etc" "$ROOT/root" "$ROOT/usr/bin" "$ROOT/bin"

copy_with_libs() {
  local src target
  src=$(readlink -f "$1")
  mkdir -p "$ROOT$(dirname "$src")"
  cp -a "$src" "$ROOT$src"
  ldd "$src" | awk '{ if ($1 ~ /^\//) print $1; else if ($3 ~ /^\//) print $3 }' | while read -r lib; do
    if [ -n "$lib" ] && [ -e "$lib" ]; then
      real=$(readlink -f "$lib")
      mkdir -p "$ROOT$(dirname "$real")" "$ROOT$(dirname "$lib")"
      cp -a "$real" "$ROOT$real"
      if [ "$real" != "$lib" ]; then
        ln -sfn "$real" "$ROOT$lib"
      fi
    fi
  done
  if [ "$1" != "$src" ]; then
    mkdir -p "$ROOT$(dirname "$1")"
    ln -sfn "$src" "$ROOT$1"
  fi
}

copy_with_libs /usr/bin/python3.10
ln -sfn python3.10 "$ROOT/usr/bin/python3"
ln -sfn /usr/bin/python3 "$ROOT/bin/python3"
copy_with_libs /lib64/ld-linux-x86-64.so.2
copy_with_libs /usr/bin/ssh
copy_with_libs /usr/bin/setsid
copy_with_libs /usr/bin/dash
ln -sfn /usr/bin/dash "$ROOT/bin/sh"

mkdir -p "$ROOT/usr/lib"
cp -a /usr/lib/python3.10 "$ROOT/usr/lib/python3.10"
find /usr/lib/python3.10/lib-dynload -name '*.so' -print | while read -r mod; do
  copy_with_libs "$mod"
done

nss=$(find /lib /usr/lib -name 'libnss_files.so.2' | head -n 1)
if [ -n "$nss" ]; then
  mkdir -p "$ROOT$(dirname "$nss")"
  cp -a "$nss" "$ROOT$nss"
fi

printf 'root:x:0:0:root:/root:/bin/sh\n' > "$ROOT/etc/passwd"
printf 'root:x:0:\n' > "$ROOT/etc/group"
printf 'hosts: files dns\npasswd: files\ngroup: files\n' > "$ROOT/etc/nsswitch.conf"

cp -a /opt/app/app.py /opt/app/known_hosts "$ROOT/opt/app/"
cp -a /opt/app/templates/. "$ROOT/opt/app/templates/"

tar -C "$ROOT" -cf /tmp/vm-panel-root.tar .
docker import \
  -c 'WORKDIR /opt/app' \
  -c 'ENV PYTHONUNBUFFERED=1' \
  -c 'EXPOSE 8080' \
  -c 'CMD ["python3", "-u", "/opt/app/app.py"]' \
  /tmp/vm-panel-root.tar vm-panel:local
rm -rf "$ROOT" /tmp/vm-panel-root.tar
docker image inspect vm-panel:local >/dev/null
echo "image vm-panel:local ready"
