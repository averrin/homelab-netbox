#!/bin/sh

# Export environment variables to a file so cron jobs can access them
# We filter out some common noise and format as export commands
printenv | grep -v -e "no_proxy" -e "PATH" > /app/env.sh
sed -i 's/^\([^=]*\)=\(.*\)$/export \1="\2"/' /app/env.sh
# Separately handle PATH to ensure we use the container's python path
echo "export PATH=$PATH" >> /app/env.sh
chmod +x /app/env.sh

# Get Cron and Cmd from env or defaults
SYNC_CRON="${SYNC_CRON:-*/2 * * * *}"
SYNC_CMD="${SYNC_CMD:-python cli.py --verbose --sources coolify,pulse,npm,proxmox --export infisical}"

# Create the crontab entry
# We source the env file, cd to app, and run the command
echo "$SYNC_CRON . /app/env.sh && cd /app && $SYNC_CMD >> /var/log/cron.log 2>&1" > /etc/cron.d/sync-cron

# Give execution rights on the cron job and apply it
chmod 0644 /etc/cron.d/sync-cron
crontab /etc/cron.d/sync-cron

# Create the log file
touch /var/log/cron.log

echo "Starting cron with expression: $SYNC_CRON"
echo "Command: $SYNC_CMD"

# Run cron in the foreground
cron -f
