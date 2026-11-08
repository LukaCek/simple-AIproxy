"""Regression tests for reproducible, fast native multiarch worker releases."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_release_uses_parallel_native_amd64_and_arm64_runners():
    workflow = yaml.safe_load((ROOT / ".github/workflows/compute-worker.yml").read_text())
    jobs = workflow["jobs"]
    smoke = jobs["docker-smoke"]
    release = jobs["build-worker"]
    publish = jobs["publish-worker"]

    expected = {
        ("linux/amd64", "ubuntu-24.04", "amd64"),
        ("linux/arm64", "ubuntu-24.04-arm", "arm64"),
    }
    for job in (smoke, release):
        actual = {
            (m["platform"], m["runner"], m["arch"])
            for m in job["strategy"]["matrix"]["include"]
        }
        assert actual == expected
        assert job["runs-on"] == "${{ matrix.runner }}"

    # Production must not compile arm64 C++ through QEMU on an x86 runner.
    release_uses = [step.get("uses", "") for step in release["steps"]]
    assert not any("setup-qemu" in action for action in release_uses)
    assert release["needs"] == ["verify", "image-changes"]
    assert publish["needs"] == "build-worker"
    assert not any("setup-qemu" in step.get("uses", "") for step in publish["steps"])


def test_release_and_pr_use_identical_per_architecture_buildkit_caches():
    workflow = yaml.safe_load((ROOT / ".github/workflows/compute-worker.yml").read_text())
    smoke, release = (workflow["jobs"][key] for key in ("docker-smoke", "build-worker"))
    smoke_build = next(step["with"] for step in smoke["steps"]
                       if "docker/build-push-action" in step.get("uses", ""))
    release_build = next(step["with"] for step in release["steps"]
                         if "docker/build-push-action" in step.get("uses", ""))
    for prop in ("cache-from", "cache-to", "platforms", "file"):
        assert release_build[prop] == smoke_build[prop], prop
    assert release_build["cache-from"] == "type=gha,scope=compute-worker-${{ matrix.arch }}"
    assert release_build["cache-to"] == "type=gha,mode=max,scope=compute-worker-${{ matrix.arch }}"


def test_manifest_published_only_after_both_immutable_digests_exist():
    workflow = yaml.safe_load((ROOT / ".github/workflows/compute-worker.yml").read_text())
    jobs = workflow["jobs"]
    release = jobs["build-worker"]
    publish = jobs["publish-worker"]
    release_build = next(step["with"] for step in release["steps"]
                         if "docker/build-push-action" in step.get("uses", ""))
    assert "push-by-digest=true" in release_build["outputs"]
    assert "name-canonical=true" in release_build["outputs"]
    assert ":latest" not in release_build["outputs"]
    assert "if-no-files-found" in str(release["steps"])
    assert any("actions/download-artifact" in step.get("uses", "")
               for step in publish["steps"])
    commands = next(step["run"] for step in publish["steps"] if "run" in step)
    assert "docker buildx imagetools create" in commands
    assert '[[ "${#digests[@]}" -ne 2 ]]' in commands
    assert '-t "$IMAGE:latest"' in commands
    assert '-t "$IMAGE:$GITHUB_SHA"' in commands
    assert "docker buildx imagetools inspect --raw" in commands


def test_build_revision_changes_only_tail_layer_not_expensive_build_dependencies():
    dockerfile = (ROOT / "worker/Dockerfile").read_text()
    assert dockerfile.index("ARG LLAMA_CPP_REF") < dockerfile.index("RUN git clone")
    assert dockerfile.index("COPY --from=llama-build") < dockerfile.index("RUN pip install")
    assert dockerfile.index("RUN pip install") < dockerfile.index("COPY compute_catalog.py")
    assert dockerfile.index("COPY compute_catalog.py") < dockerfile.index("ARG WORKER_BUILD_REVISION")
    assert dockerfile.index("ARG WORKER_BUILD_REVISION") < dockerfile.index('CMD ["python", "agent.py"]')


def test_docs_and_installer_only_changes_skip_expensive_native_build():
    import re

    workflow = yaml.safe_load((ROOT / ".github/workflows/compute-worker.yml").read_text())
    jobs = workflow["jobs"]
    gate = jobs["image-changes"]
    smoke = jobs["docker-smoke"]
    release = jobs["build-worker"]
    assert "image-changes" in smoke["needs"]
    assert "image-changes" in release["needs"]
    assert "needs.image-changes.outputs.rebuild == 'true'" in smoke["if"]
    assert "needs.image-changes.outputs.rebuild == 'true'" in release["if"]

    script = next(step["run"] for step in gate["steps"] if step.get("id") == "paths")
    match = re.search(r"grep -Eq '([^']+)'", script)
    assert match is not None
    pattern = re.compile(match.group(1))
    for changed in (
        "worker/Dockerfile", "worker/agent.py", "worker/tui.py",
        "worker/requirements.txt", "compute_catalog.py",
        "compute_worker_storage.py", "compute_worker_snapshot.py",
        ".github/workflows/compute-worker.yml",
    ):
        assert pattern.search(changed), changed
    for unchanged_image in (
        "worker/install.sh", "worker/auto-update.sh", "worker/README.md",
        "compute_plane.py", "templates/compute_setup.html",
        "tests/test_worker_build_pipeline.py",
    ):
        assert not pattern.search(unchanged_image), unchanged_image
    assert "EVENT_NAME" in script and "workflow_dispatch" in script
    assert "rebuild=true" in script and "rebuild=false" in script
