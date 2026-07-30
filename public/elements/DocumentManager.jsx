import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { FileText, FileCog, Database, FileUp, Eye, Trash2 } from "lucide-react"
import { useState } from "react"

const STAGE_LABEL = {
  en: {
    uploaded: "Uploaded",
    preprocessed: "Preprocessed",
    ingested: "Ingested",
  },
  de: {
    uploaded: "Hochgeladen",
    preprocessed: "Vorverarbeitet",
    ingested: "Ingestiert",
  },
}

const STAGE_ICON = {
  uploaded: FileUp,
  preprocessed: FileCog,
  ingested: Database,
}

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

  const groups = ["uploaded", "preprocessed", "ingested"]
    .map((stage) => ({
      stage,
      items: documents.filter((d) => d.stage === stage),
    }))
    .filter((g) => g.items.length > 0)

  if (groups.length === 0) {
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

  return (
    <Card className="w-full">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium flex items-center gap-2">
          <FileText className="h-4 w-4" />
          {lang === "en" ? "Documents" : "Dokumente"}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        {groups.map((group) => {
          const Icon = STAGE_ICON[group.stage] || FileText
          return (
            <div key={group.stage} className="space-y-1">
              <div className="flex items-center gap-1.5 font-medium text-muted-foreground">
                <Icon className="h-3.5 w-3.5" />
                {STAGE_LABEL[lang][group.stage]} ({group.items.length})
              </div>
              <div className="space-y-1">
                {group.items.map((d) => {
                  const openBusy = busy.open.has(d.id)
                  const preBusy = busy.preprocess.has(d.id)
                  const delBusy = busy.delete.has(d.id)
                  return (
                    <div
                      key={d.id}
                      className="flex items-center justify-between rounded border px-2 py-1"
                    >
                      <span className="truncate font-mono text-[11px]" title={d.name}>
                        {d.name}
                      </span>
                      <div className="ml-2 shrink-0 flex items-center gap-1">
                        <span className="text-muted-foreground">
                          {formatBytes(d.bytes)}
                        </span>
                        {d.stage !== "ingested" && (
                          <button
                            type="button"
                            title={d.canOpen === false ? labels.openDisabled : labels.open}
                            disabled={openBusy || d.canOpen === false}
                            onClick={() => handleOpen(d.id)}
                            className="p-1 rounded hover:bg-accent disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                          >
                            <Eye className="h-3.5 w-3.5" />
                          </button>
                        )}
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
                    </div>
                  )
                })}
              </div>
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}