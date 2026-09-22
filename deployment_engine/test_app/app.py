from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        message = "Module 3 Docker deployment works!"

        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

        self.wfile.write(message.encode())

    def log_message(self, format, *args):
        pass


server = HTTPServer(("0.0.0.0", 8000), Handler)

print("Test application running on port 8000")

server.serve_forever()