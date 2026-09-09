# Bridge icon assets

- `bridge-icon-master.png`: selected circular artwork, 1254 × 1254 RGBA PNG.
- `bridge-icon.png`: 64 × 64 RGBA export, 7,253 bytes. Use this same file for MCP metadata, the README, and the ChatGPT app-logo upload field.

The circular artwork fills the square canvas, with transparent corners. The selected design has a blue–purple gradient and a centered white bridge mark. Preserve this selected source when exporting other sizes.

Export the compact version on macOS:

```sh
sips -z 64 64 assets/bridge-icon-master.png --out assets/bridge-icon.png
```

`bridge/mcp_server.py` embeds the compact PNG as a data URI so MCP clients do not require a separate image host. When replacing the export, update the embedded bytes and the pinned hash in `tests/test_mcp.py`. The initialize test also checks that the embedded bytes match this file, that the PNG retains RGBA, and that its size stays within 10 KB.

SHA-256:

- Master: `1bb0a36afcb5bec0cea10c7bda7ec84b557e00e33e383cc54083683411c518b3`
- Compact: `a3d104c021b78db2c5c271fcfaf2d10772fc7c1fedc6483d4a55f6f04fed8ac5`

Changing the MCP icon does not replace a separately uploaded ChatGPT app logo. The existing app must use the compact file before client-side acceptance can be completed.
