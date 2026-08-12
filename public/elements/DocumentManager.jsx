import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { FileText, FileCog, Eye, Trash2, Check, X } from "lucide-react"
import { useState } from "react"

// Default tooltips / confirm used when the Python side omits a ``labels``
// block (older builds / partial props). Kept in sync with i18n.py's
// ``doc.action.*`` keys.
const DEFAULT_LABELS = {
  open: "Preview this document inline",
  openDisabled:
    "Inline preview isn't available for this file type. Preprocess it to Markdown first.",
  preprocess: "Convert this document to Markdown via docprep",
  delete: "Delete this document and its on-disk file",
  deleteConfirm:
    "Delete this document? The on-disk file will be removed. Ingested rows are permanent and cannot be deleted.",
}

function formatBytes(bytes) {
  if (bytes == null) return ""
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function YesNo({ value }) {
  return value ? (
    <Check className="h-3.5 w-3.5 text-emerald-600" aria-label="yes" />
  ) : (
    <X className="h-3.5 w-3.5 text-muted-foreground" aria-label="no" />
  )
}

export default function DocumentManager() {
  const documents = props.documents || []
  const lang = props.lang === "en" ? "en" : "de"
  const labels = { ...DEFAULT_LABELS, ...(props.labels || {}) }
  const [busy, setBusy] = useState({ open: new Set(), preprocess: new Set(), delete: new Set() })

  async function runAction(kind, id) {
    if (busy[kind].has(id)) return
    setBusy({ ...busy, [kind]: new Set([...busy[kind], id]) })
    try {
      const name =
        kind === "open"
          ? "open_document"
          : kind === "preprocess"
            ? "preprocess_document_action"
            : "delete_document"
      await callAction({ name, payload: { id } })
    } finally {
      setTimeout(() => {
        setBusy((prev) => {
          const next = new Set(prev[kind])
          next.delete(id)
          return { ...prev, [kind]: next }
        })
      }, 400)
    }
  }

  function handleOpen(id) {
    runAction("open", id)
  }

  function handlePreprocess(id) {
    runAction("preprocess", id)
  }

  function handleDelete(id) {
    if (!window.confirm(labels.deleteConfirm)) return
    runAction("delete", id)
  }

  if (documents.length === 0) {
    return (
      <Card className="w-full">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium flex items-center gap-2">
            <FileText className="h-4 w-4" />
            {lang === "en" ? "Documents" : "Dokumente"}
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xs text-muted-foreground">
          {lang === "en" ? "No documents yet." : "Noch keine Dokumente."}
        </CardContent>
      </Card>
    )
  }

  const headers = lang === "en"
    ? { name: "File", preprocessed: "Preprocessed", ingested: "Ingested", size: "Size", actions: "" }
    : { name: "Datei", preprocessed: "Vorverarbeitet", ingested: "Ingestiert", size: "Größe", actions: "" }

  return (
    <Card className="w-full">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium flex items-center gap-2">
          <FileText className="h-4 w-4" />
          {lang === "en" ? "Documents" : "Dokumente"}
        </CardTitle>
      </CardHeader>
      <CardContent className="text-xs p-0">
        <table className="w-full border-collapse">
          <thead>
            <tr className="border-b font-medium text-muted-foreground bg-muted/40">
              <th className="text-left px-2 py-1.5 w-full">{headers.name}</th>
              <th className="text-center px-2 py-1.5 whitespace-nowrap">{headers.preprocessed}</th>
              <th className="text-center px-2 py-1.5 whitespace-nowrap">{headers.ingested}</th>
              <th className="text-right px-2 py-1.5 whitespace-nowrap">{headers.actions}</th>
            </tr>
          </thead>
          <tbody>
            {documents.map((d) => {
              const openBusy = busy.open.has(d.id)
              const preBusy = busy.preprocess.has(d.id)
              const delBusy = busy.delete.has(d.id)
              return (
                <tr key={d.id} className="border-b last:border-b-0">
                  <td className="px-2 py-1">
                    <div className="min-w-0 flex items-center gap-1.5">
                      <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      <span className="truncate font-mono text-[11px]" title={d.name}>
                        {d.name}
                      </span>
                      <span className="text-muted-foreground shrink-0">
                        {formatBytes(d.bytes)}
                      </span>
                    </div>
                  </td>
                  <td className="text-center px-2 py-1">
                    <YesNo value={d.preprocessed} />
                  </td>
                  <td className="text-center px-2 py-1">
                    <YesNo value={d.ingested} />
                  </td>
                  <td className="text-right px-2 py-1">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        type="button"
                        title={d.canOpen === false ? labels.openDisabled : labels.open}
                        disabled={openBusy || d.canOpen === false}
                        onClick={() => handleOpen(d.id)}
                        className="p-1 rounded hover:bg-accent disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                      >
                        <Eye className="h-3.5 w-3.5" />
                      </button>
                      {d.canPreprocess && (
                        <button
                          type="button"
                          title={labels.preprocess}
                          disabled={preBusy}
                          onClick={() => handlePreprocess(d.id)}
                          className="p-1 rounded hover:bg-accent disabled:opacity-50"
                        >
                          <FileCog className="h-3.5 w-3.5" />
                        </button>
                      )}
                      {d.deletable !== false && (
                        <button
                          type="button"
                          title={labels.delete}
                          disabled={delBusy}
                          onClick={() => handleDelete(d.id)}
                          className="p-1 rounded hover:bg-accent disabled:opacity-50"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </CardContent>
    </Card>
  )
}