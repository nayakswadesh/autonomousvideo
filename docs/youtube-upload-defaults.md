# Private YouTube upload defaults

The uploader uses the finalized publication package, not the first thumbnail candidate:

1. The project must be completed, with a cleared release for its current version.
2. `publication.thumbnail_artifact_id` identifies the finalized thumbnail.
3. The root `selected-thumbnail.png` must match that artifact's SHA-256 before upload.
4. `thumbnails.set` sends that exact file after YouTube accepts the video. A thumbnail failure is recorded separately; retry uploads only the thumbnail, not another video.

Read-only inspection of completed `99a941f3caca` confirmed its publication approval and a matching root thumbnail hash. Regression tests also select a later candidate and verify that this one, not the earlier candidate, reaches `thumbnails.set`. A modified root thumbnail is rejected before any video upload.

For these English-language productions, the upload request sets:

- `snippet.defaultLanguage: "en"` (metadata language).
- `snippet.defaultAudioLanguage: "en"` (spoken language, taken from the saved voice specification).
- `paidProductPlacementDetails.hasPaidProductPlacement: false` (no paid promotion).
- `status.privacyStatus: "private"` and `status.selfDeclaredMadeForKids: false` remain unchanged.

`videos.insert` includes `snippet,status,paidProductPlacementDetails` in `part`. Already completed packages receive missing defaults in memory without rewriting their approved files/hashes. Explicit paid-promotion declarations are not silently overwritten. The upload-start event records the audio language, promotion declaration and thumbnail hash.

References checked against Google's official documentation and the installed Google API discovery schema:

- [Video resource fields](https://developers.google.com/youtube/data/reference/rest/v3/videos)
- [Upload method and parts](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert)
- [Thumbnail upload method](https://developers.google.com/youtube/data/reference/rest/v3/thumbnails/set)

Local tests do not prove YouTube's eventual presentation or Studio UI state. No live upload or modification of previously uploaded videos is part of this change; verify the next private upload in Studio.
