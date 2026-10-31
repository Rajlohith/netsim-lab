// Turn a Blob into a browser download. Separate from the API client so it can be replaced in tests.
export function saveBlob(blob, filename, { doc = globalThis.document, urlApi = globalThis.URL } = {}) {
  const url = urlApi.createObjectURL(blob);
  try {
    const a = doc.createElement("a");
    a.href = url;
    a.download = filename;
    a.rel = "noopener";
    doc.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    urlApi.revokeObjectURL(url);
  }
}

export function saveText(text, filename, mime = "text/plain;charset=utf-8", opts) {
  saveBlob(new Blob([text], { type: mime }), filename, opts);
}
