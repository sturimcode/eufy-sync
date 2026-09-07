# Releasing Eufy Sync

PRs and version tags run the lint and test jobs in `.github/workflows/checks.yml`. The `CI` check on a PR passes only when every shared job passes. Branch protection requires this check from GitHub Actions and an up-to-date branch, including for administrators.

## Publish a version

1. Update the version in `pyproject.toml` and `eufy_sync/__init__.py` in the same PR as the release changes.
2. Merge after `CI` passes, then update the local `main` checkout.
3. Tag the merged release commit with `v` followed by the package version and push that tag.
4. Watch the **Publish to PyPI** workflow. It runs the shared checks, uploads the package, then creates the GitHub release.

The publish job checks that the tag agrees with both version declarations. Manual runs must also use a version tag. Runs for the same tag are serialized so retries cannot publish concurrently.

## Recover a failed release

If PyPI succeeds but GitHub release creation fails, open that workflow run and choose **Re-run failed jobs**, or run:

```sh
gh run rerun RUN_ID --failed --repo sturimcode/eufy-sync
```

PyPI upload and GitHub release creation are separate jobs, so this retries the failed GitHub job without repeating a successful upload. An existing published release keeps its notes; an existing draft is published with its notes intact.

Use the same tag and workflow run for recovery. PyPI still rejects duplicate files, so rerunning every job after a successful upload will stop at that check. If the PyPI job itself fails, inspect its log and the package page before retrying: an upload can fail after only some distribution files have reached PyPI.
