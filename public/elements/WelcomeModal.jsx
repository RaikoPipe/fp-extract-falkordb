import { Button } from "@/components/ui/button"
import { Card, CardHeader, CardTitle, CardContent, CardFooter } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Markdown } from "@/components/markdown"
import { ShieldAlert } from "lucide-react"
import { useEffect, useState } from "react"

// Centered modal overlay rendered via position:fixed so it floats above the
// chat regardless of scroll. A dimmed backdrop blocks interaction with the
// page behind it. The ONLY way to close the modal is the "I understand and
// acknowledge." button — backdrop click and Escape are intentionally ignored.
//
// Persistence is client-side via localStorage: once acknowledged, the modal
// is not shown again for that browser until the storage key changes
// (bump ``dismissedKey`` version on the Python side to re-show after a
// future edit of the warning text).
export default function WelcomeModal() {
  const title = props.title || (props.lang === "en" ? "Test Build — Please Read" : "Test-Build — Bitte lesen")
  const intro = props.intro || ""
  const risks = Array.isArray(props.risks) ? props.risks : []
  const closing = props.closing || ""
  const ackLabel =
    props.ackLabel || (props.lang === "en" ? "I understand and acknowledge." : "Ich verstehe und bestätige dies.")
  const dismissedKey = props.dismissedKey || "fp_welcome_ack_v1"

  const [open, setOpen] = useState(false)

  useEffect(() => {
    try {
      if (window.localStorage.getItem(dismissedKey) === "ack") {
        setOpen(false)
        return
      }
    } catch {
      // localStorage may be unavailable (private mode / disabled); show the
      // modal in that case so the warning is never silently skipped.
    }
    setOpen(true)
  }, [dismissedKey])

  async function handleAck() {
    try {
      window.localStorage.setItem(dismissedKey, "ack")
    } catch {
      // Best-effort persistence; hide locally regardless.
    }
    setOpen(false)
    try {
      await callAction({ name: "acknowledge_welcome", payload: {} })
    } catch {
      // Action callback is optional; the modal is already hidden.
    }
  }

  if (!open) return null

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 100000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "1rem",
        backgroundColor: "rgba(0, 0, 0, 0.6)",
      }}
    >
      <Card
        className="w-full max-w-2xl max-h-[90vh] flex flex-col"
        style={{ boxShadow: "0 10px 40px rgba(0,0,0,0.5)" }}
      >
        <CardHeader className="pb-3 shrink-0">
          <CardTitle className="text-lg font-semibold flex items-center gap-2">
            <ShieldAlert className="h-5 w-5 text-amber-500" />
            {title}
          </CardTitle>
        </CardHeader>
        <CardContent className="overflow-y-auto space-y-4 text-sm">
          {intro && (
            <Markdown className="text-muted-foreground prose prose-sm max-w-none">
              {intro}
            </Markdown>
          )}
          <ul className="space-y-3">
            {risks.map((risk, i) => (
              <li key={i} className="space-y-1">
                <div className="flex items-start gap-2">
                  <Badge variant="outline" className="shrink-0 border-amber-500/50 text-amber-600">
                    {i + 1}
                  </Badge>
                  <div className="space-y-1">
                    <div className="font-medium">{risk.title}</div>
                    <Markdown className="text-muted-foreground prose prose-sm max-w-none">
                      {risk.body}
                    </Markdown>
                  </div>
                </div>
              </li>
            ))}
          </ul>
          {closing && (
            <Markdown className="font-medium prose prose-sm max-w-none pt-2 border-t">
              {closing}
            </Markdown>
          )}
        </CardContent>
        <CardFooter className="shrink-0 pt-2 flex justify-end">
          <Button onClick={handleAck} className="gap-2">
            <ShieldAlert className="h-4 w-4" />
            {ackLabel}
          </Button>
        </CardFooter>
      </Card>
    </div>
  )
}