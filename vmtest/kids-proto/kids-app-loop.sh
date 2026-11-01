#!/bin/bash
# Prototype app loop: keep OtterTube running; log each exit.
while true; do
    echo "$(date +%T) start" >> /tmp/kids-app.log
    /app/ottertube-desktop --no-sandbox --ozone-platform=wayland --disable-gpu >> /tmp/kids-app.log 2>&1
    echo "$(date +%T) exit $?" >> /tmp/kids-app.log
    sleep 3
done
