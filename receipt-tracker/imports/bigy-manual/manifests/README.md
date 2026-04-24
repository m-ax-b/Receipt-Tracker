# Big Y Fetch Manifest Notes

Each fetched receipt should have a JSON manifest alongside the artifact(s).

Suggested fields:
- merchant
- fetched_at
- source_url
- browser_profile
- artifact_paths
- artifact_type
- notes

Example artifact types:
- screenshot_png
- image_file
- html_page
- mixed_bundle

This folder is for retrieval metadata only.
Interpretation happens later in the receipt-tracker app.

