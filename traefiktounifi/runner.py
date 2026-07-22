import time

import schedule

from traefiktounifi import app, webui

# Start the optional read-only web UI (no-op unless WEB_UI_ENABLED).
webui.start_web_ui()

traefik_to_unifi = app.TraefikToUnifi()
traefik_to_unifi.sync()

# Schedule the sync function to run every minute
schedule.every(1).minutes.do(traefik_to_unifi.sync)

# Keep the script running indefinitely
while True:
    schedule.run_pending()
    time.sleep(1)
