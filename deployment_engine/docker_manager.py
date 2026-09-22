import socket

import docker


class DockerManager:
    def __init__(self):
        self.client = docker.from_env()

    def build_image(self, image_name: str, source_path: str):
        print(f"Building Docker image: {image_name}")

        image, logs = self.client.images.build(
            path=source_path,
            tag=image_name,
        )

        for log in logs:
            if "stream" in log:
                print(log["stream"].strip())

        return image

    def find_free_port(self, start_port: int = 8000) -> int:
        """
        Find an available TCP port on the host machine.
        """
        port = start_port

        while True:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                result = sock.connect_ex(("127.0.0.1", port))

            if result != 0:
                return port

            port += 1

    def run_container(
        self,
        image_name: str,
        container_name: str,
        host_port: int,
        container_port: int = 8000,
        env_vars: dict | None = None,
    ):
        print(f"Starting container: {container_name}")

        container = self.client.containers.run(
            image_name,
            name=container_name,
            detach=True,
            environment=env_vars or {},
            ports={
                f"{container_port}/tcp": host_port,
            },
        )

        return container

    def get_container_status(self, container_name: str):
        container = self.client.containers.get(container_name)
        container.reload()
        return container.status

    def get_container_logs(self, container_name: str):
        container = self.client.containers.get(container_name)

        return container.logs().decode("utf-8")

    def stop_container(self, container_name: str):
        container = self.client.containers.get(container_name)
        container.stop()

    def remove_container(self, container_name: str):
        container = self.client.containers.get(container_name)

        if container.status == "running":
            container.stop()

        container.remove()