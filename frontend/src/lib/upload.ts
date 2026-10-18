// Staged upload: small files go up in multipart batches (keeping folder paths); large files (zips)
// go up in chunks with byte offsets, so a dropped connection costs one chunk, not the whole file.

import { api } from "./api";

const BATCH_BYTES = 24 * 1024 * 1024;
const CHUNK_BYTES = 16 * 1024 * 1024;
const LARGE = 64 * 1024 * 1024;

export interface PickedFile {
  file: File;
  path: string;
}

export function pickedFromInput(list: FileList | null): PickedFile[] {
  return Array.from(list ?? []).map((f) => ({ file: f, path: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name }));
}

/** Files from a drag-and-drop, walking dropped folders. */
export async function pickedFromDrop(e: DragEvent | React.DragEvent): Promise<PickedFile[]> {
  const items = Array.from(e.dataTransfer?.items ?? []);
  const entries = items.map((i) => i.webkitGetAsEntry?.()).filter(Boolean) as FileSystemEntry[];
  if (!entries.length) return Array.from(e.dataTransfer?.files ?? []).map((f) => ({ file: f, path: f.name }));
  const out: PickedFile[] = [];
  const walk = async (entry: FileSystemEntry, prefix: string): Promise<void> => {
    if (entry.isFile) {
      const f = await new Promise<File>((res, rej) => (entry as FileSystemFileEntry).file(res, rej));
      out.push({ file: f, path: prefix + entry.name });
    } else if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      let batch: FileSystemEntry[];
      do {
        batch = await new Promise<FileSystemEntry[]>((res, rej) => reader.readEntries(res, rej));
        for (const child of batch) await walk(child, `${prefix}${entry.name}/`);
      } while (batch.length);
    }
  };
  for (const en of entries) await walk(en, "");
  return out;
}

export async function uploadFiles(files: PickedFile[], onProgress: (sent: number, total: number) => void): Promise<string> {
  const { upload_id } = await api.post<{ upload_id: string }>("/uploads");
  const total = files.reduce((a, f) => a + f.file.size, 0);
  let sent = 0;
  const small = files.filter((f) => f.file.size < LARGE);
  const large = files.filter((f) => f.file.size >= LARGE);
  let batch: PickedFile[] = [];
  let batchBytes = 0;
  const flush = async () => {
    if (!batch.length) return;
    const fd = new FormData();
    for (const f of batch) {
      fd.append("files", f.file, f.file.name);
      fd.append("paths", f.path);
    }
    await api.post(`/uploads/${upload_id}/files`, fd);
    sent += batchBytes;
    onProgress(sent, total);
    batch = [];
    batchBytes = 0;
  };
  for (const f of small) {
    batch.push(f);
    batchBytes += f.file.size;
    if (batchBytes >= BATCH_BYTES) await flush();
  }
  await flush();
  for (const f of large) {
    for (let off = 0; off < f.file.size; off += CHUNK_BYTES) {
      const blob = f.file.slice(off, off + CHUNK_BYTES);
      const r = await fetch(`/api/uploads/${upload_id}/chunk?path=${encodeURIComponent(f.path)}&offset=${off}`, { method: "PUT", body: blob });
      if (!r.ok) throw new Error(`upload of ${f.path} failed at ${off}: ${r.status}`);
      sent += blob.size;
      onProgress(sent, total);
    }
  }
  return upload_id;
}
