from pathlib import Path
import shutil

from fastapi import Depends, FastAPI

from deployment_engine.auth import verify_internal_token
from deployment_engine.docker_manager import DockerManager
from deployment_engine.git_manager import clone_repository, GitError
from deployment_engine.schemas import DeploymentRequest


app = FastAPI(title="Mini Cloud Deployment Engine")

docker_manager = DockerManager()

# Temporary workspace for cloned repositories
WORKSPACE = Path("deployment_workspace")
WORKSPACE.mkdir(parents=True, exist_ok=True)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/deploy")
async def deploy(
    request: DeploymentRequest,
    _: None = Depends(verify_internal_token),
):
    deployment_id = request.deployment_id

    # ---------------------------------------------------------
    # 1. Create deployment workspace
    # ---------------------------------------------------------

    deployment_path = WORKSPACE / deployment_id

    if deployment_path.exists():
        shutil.rmtree(deployment_path)

    deployment_path.mkdir(parents=True, exist_ok=True)

    try:
        # -----------------------------------------------------
        # 2. Clone GitHub repository
        # -----------------------------------------------------

        print(f"[{deployment_id}] Cloning repository...")

        clone_repository(
            clone_url=request.repository.clone_url,
            branch=request.repository.branch,
            destination=deployment_path,
        )

        print(f"[{deployment_id}] Repository cloned.")

        # -----------------------------------------------------
        # 3. Check Dockerfile
        # -----------------------------------------------------

        dockerfile = deployment_path / "Dockerfile"

        if not dockerfile.exists():
            return {
                "engine_job_id": deployment_id,
                "status": "failed",
                "error": "Dockerfile not found in repository.",
            }

        # -----------------------------------------------------
        # 4. Create Docker image
        # -----------------------------------------------------

        image_name = f"deployment-{deployment_id}".lower()

        # Docker image names cannot contain some characters.
        image_name = image_name.replace("_", "-")

        print(f"[{deployment_id}] Building image...")

        docker_manager.build_image(
            image_name=image_name,
            source_path=str(deployment_path),
        )

        print(f"[{deployment_id}] Image built successfully.")

        # -----------------------------------------------------
        # 5. Find available host port
        # -----------------------------------------------------

        host_port = docker_manager.find_free_port(8000)

        print(
            f"[{deployment_id}] Using host port {host_port}"
        )

        # -----------------------------------------------------
        # 6. Prepare environment variables
        # -----------------------------------------------------

        environment = {}

        for variable in request.env_vars:
            environment[variable.key] = variable.value

        # -----------------------------------------------------
        # 7. Start Docker container
        # -----------------------------------------------------

        container_name = f"deployment-{deployment_id}".lower()
        container_name = container_name.replace("_", "-")

        print(
            f"[{deployment_id}] Starting container..."
        )

        container = docker_manager.run_container(
            image_name=image_name,
            container_name=container_name,
            host_port=host_port,
            container_port=8000,
            env_vars=environment,
        )

        # -----------------------------------------------------
        # 8. Get container status
        # -----------------------------------------------------

        status = docker_manager.get_container_status(
            container_name
        )

        print(
            f"[{deployment_id}] Container status: {status}"
        )

        # -----------------------------------------------------
        # 9. Generate live URL
        # -----------------------------------------------------

        live_url = f"http://localhost:{host_port}"

        return {
            "engine_job_id": deployment_id,
            "status": status,
            "container_id": container.id,
            "live_url": live_url,
            "host_port": host_port,
            "container_port": 8000,
        }

    except GitError as error:

        return {
            "engine_job_id": deployment_id,
            "status": "failed",
            "error": f"Git clone failed: {str(error)}",
        }

    except Exception as error:

        print(
            f"[{deployment_id}] Deployment failed: {error}"
        )

        return {
            "engine_job_id": deployment_id,
            "status": "failed",
            "error": str(error),
        }