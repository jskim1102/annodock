import { downloadResponse } from "../api/client";
import { artifactUrl, type ArtifactName } from "../api/training";

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function downloadBlob(path: string, filename: string) {
  saveBlob(await downloadResponse(path), filename);
}

export async function downloadArtifact(runId: number, name: ArtifactName) {
  await downloadBlob(artifactUrl(runId, name), name);
}
