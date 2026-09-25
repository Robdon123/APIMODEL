from cloud_app import app
from cloud_auth import install_auth
from cloud_tasks import start_background_sync

install_auth(app)
start_background_sync()
