import { Button } from "@/components/ui/button"
import { PanelRight } from "lucide-react"
import { useState } from "react"

export default function OpenDocsButton() {
  const label = props.label || (props.lang === "en" ? "Documents" : "Dokumente")
  const title =
    props.title ||
    (props.lang === "en" ? "Open document sidebar" : "Dokumenten-Seitenleiste öffnen")
  const [busy, setBusy] = useState(false)

  async function handleClick() {
    if (busy) return
    setBusy(true)
    try {
      await callAction({ name: "open_document_sidebar", payload: {} })
    } finally {
      setTimeout(() => setBusy(false), 400)
    }
  }

  return (
    <div
      style={{
        position: "fixed",
        bottom: "1rem",
        right: "1rem",
        zIndex: 9999,
      }}
    >
      <Button
        size="sm"
        variant="outline"
        title={title}
        disabled={busy}
        onClick={handleClick}
        className="shadow-md gap-1.5"
      >
        <PanelRight className="h-4 w-4" />
        {label}
      </Button>
    </div>
  )
}