"""Spawn-safe entry point for the Cloud Run execution worker."""

if __name__ == "__main__":
    from app.cloud_worker import main

    main()
