# How to Transfer Deployment Package to VPS

## Option 1: Using SCP (Recommended - Fastest)

If you have the deployment package on your local computer:

```bash
# On your local computer
scp deployment/mastra-deployment.tar.gz root@147.182.239.195:/opt/polymarket-bot/
```

## Option 2: Via Replit Web Server

This works if you can't use scp:

```bash
# 1. Start web server in Replit
cd ~/workspace
python3 -m http.server 8080
```

Then:
- Click on "Webview" in Replit
- Navigate to the URL shown
- Add `:8080/deployment/mastra-deployment.tar.gz` to the URL
- Download the file to your computer
- Upload to VPS using FileZilla or another SFTP client

## Option 3: Direct wget (If Replit is publicly accessible)

```bash
# On VPS
cd /opt/polymarket-bot
wget <YOUR_REPLIT_WEBVIEW_URL>:8080/deployment/mastra-deployment.tar.gz
```

Replace `<YOUR_REPLIT_WEBVIEW_URL>` with your actual Replit webview URL.

## After Transfer

```bash
# On VPS
cd /opt/polymarket-bot
mkdir -p mastra
cd mastra
tar -xzf ../mastra-deployment.tar.gz

# Now follow README.md for installation
bash install.sh
```
