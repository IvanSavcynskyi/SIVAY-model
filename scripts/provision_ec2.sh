#!/usr/bin/env bash
# Run on a fresh Ubuntu EC2 host as its sudo-capable ubuntu user.
set -euo pipefail
sudo apt-get update -qq
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
if [ -z "$(sudo swapon --show --noheadings)" ] && [ ! -e /swapfile ]; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    if ! sudo grep -q '^/swapfile ' /etc/fstab; then
        printf '/swapfile none swap sw 0 0\n' | sudo tee -a /etc/fstab > /dev/null
    fi
fi
