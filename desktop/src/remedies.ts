/** The remedy sentence per named error — the quick-start §10 convention, in-app. */
export const REMEDIES: Record<string, string> = {
  ports_in_use:
    "Port 5173 or 8000 is already used by another program — close that program (the operator guide's troubleshooting table says how to find it), then choose Start stack (retry).",
  pull_failed:
    "The service images could not be downloaded — check the internet connection, then choose Start stack (retry). Nothing is left half-started.",
  up_failed:
    "The stack refused to start — open View logs and read the last compose lines; contact support with them if unclear.",
  migration_failed:
    "The database setup failed — the app stays stopped on purpose. Open View logs and send the last lines to support.",
  stop_failed: "The stack could not be stopped cleanly — View logs has the compose output.",
  status_failed: "The stack state could not be read — View logs has the compose output.",
  stack_not_ready:
    "This action needs the stack Ready — choose Start stack, wait for the green state, then retry.",
  pg_dump_failed:
    "The database export failed — View logs has the error; the archive was not written.",
  mirror_failed:
    "The document-store mirror failed — View logs has the error; the archive was not written.",
  copy_failed:
    "The backed-up files could not be copied out of the container — View logs has the error.",
  archive_failed: "The backup archive could not be written — check disk space in the destination.",
  download_failed:
    "The update could not be downloaded — check the connection and retry; the running stack is untouched.",
  digest_mismatch:
    "The downloaded installer's checksum does not match the release manifest — do NOT run it; retry later or contact support.",
  readiness_timeout:
    "The services started but the app did not answer in time — open View logs and look at the last compose lines; then Stop stack and Start stack (retry).",
};
