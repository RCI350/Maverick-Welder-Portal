import os
from dotenv import load_dotenv
load_dotenv()

DATABASE_URL   = os.environ['DATABASE_URL']
SECRET_KEY     = os.environ['SECRET_KEY']
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
WEBHOOK_URL    = os.environ.get('WEBHOOK_URL', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')
