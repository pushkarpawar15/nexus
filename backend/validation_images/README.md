# Local validator images

The PayFlow target checkout is separate from NEXUS. Build images from its root:

```sh
python3 backend/validation_images/build_images.py /absolute/path/to/target-checkout
```

The builder accepts only these four target files: `backend/requirements.txt`,
`backend/requirements-dev.txt`, `frontend/package.json`, and
`frontend/package-lock.json`. It rejects symlinks, files larger than 15 MiB,
and credential-looking values. Temporary Docker contexts contain those
manifests and the NEXUS-owned Dockerfile only; they are removed after the
image builds finish.

The resulting tags are `nexus-payflow-python-validator:local` and
`nexus-payflow-node-validator:local`. Build them again when target dependencies
change. Runtime validation uses these prebuilt images without installing
packages or accessing the network.
