from flask import Flask
from dotenv import load_dotenv


def create_app():
    load_dotenv()
    app = Flask(__name__)

    from . import routes
    routes.register(app)

    return app
