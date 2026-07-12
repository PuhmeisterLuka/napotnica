def register(app):
    @app.get("/")
    def index():
        return "napotnica"
