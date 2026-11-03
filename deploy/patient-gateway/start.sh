#!/bin/sh
# Fixed command only. Even nginx startup certificate/path diagnostics stay private.
nginx -e /dev/null -g 'daemon off;' >/dev/null 2>&1
status=$?
if [ "$status" -ne 0 ]; then
    printf '%s\n' 'Patient gateway unavailable.' >&2
fi
exit "$status"
