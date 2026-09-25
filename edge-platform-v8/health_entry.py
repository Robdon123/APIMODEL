from app import app

@app.get('/health')
def health():
    return {'ok': True, 'service': 'APIMODEL', 'version': 'v8-cloud-bridge'}
