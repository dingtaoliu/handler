# Chat images (vision)

Images attached to a chat message are shown to the model, in both harnesses,
and also kept as workspace files.

## Flow

1. `POST /api/uploads` (multipart, field `files`, max 10 files and 20 MB each)
   stores the file. A PNG/JPEG/WebP/GIF is recorded with the type its bytes
   say (`images.sniff`), not the client's claim. Pasted screenshots often
   arrive as `image.png` with a generic or wrong type.
2. `POST /api/chat {text, attachments: [ids]}` checks that each id belongs to
   the caller. It answers 422 above 6 images per message.
3. At run time (`RunExecutor._attachments`) every attachment is copied to
   `attachments/<name>` in the thread workspace. Files whose bytes are a
   vision type become `RunRequest.images`, which point at the workspace copies:
   - `openai_agents`: the turn input becomes one user message with an
     `input_text` part and an `input_image` data URL per image. The MIME type
     comes from the bytes.
   - `codex`: each image is passed as `-i <path>`.
   The prompt's `Attached files:` line lists every file and marks the images
   shown to the model, so later turns can reopen them by path.
4. The transcript keeps `{"type": "file", file_id, filename, content_type}`
   blocks on the user message. Clients render image blocks from
   `GET /api/uploads/<id>`, which also returns `X-Handler-Upload-Owner`. A proxy
   that acts for one user with the admin key, such as Crontal, uses that header
   to refuse other users' files, because an admin principal can read every
   upload.

## Limits

| Limit | Value | Where |
|---|---|---|
| Vision types | PNG, JPEG, WebP, GIF (by magic bytes) | `images.VISION_TYPES` |
| Images per message | 6. Rejected at `/api/chat`; any extra images in a turn stay files only | `images.MAX_IMAGES_PER_MESSAGE` |
| Image size for the model | 10 MB. Larger images are downscaled to a longest side of 2048 px or less with Pillow and saved as `attachments/<stem>.downscaled.jpg|png`. The original is kept | `images.MAX_IMAGE_BYTES`, `images.downscale` |

SVG, HEIC, BMP and similar types, and files that claim `image/*` but whose
bytes are not a vision type, are attached as files only.
