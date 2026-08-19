import { Card, CardContent } from "@/components/ui/card"
import { Circle, LoaderCircle, CheckCircle2, ListTodo, Gauge } from "lucide-react"

const STATUS_ICONS = {
  pending: Circle,
  in_progress: LoaderCircle,
  completed: CheckCircle2,
}

const STATUS_CLASSES = {
  pending: "text-muted-foreground",
  in_progress: "text-amber-500 animate-spin",
  completed: "text-emerald-600",
}

function formatRate(rate) {
  if (rate == null || rate <= 0) return "?"
  return `${rate.toFixed(2)} it/s`
}

function formatPercent(pct) {
  if (pct == null) return "?"
  return `${pct.toFixed(0)}%`
}

// Stages that never emit granular ``progress`` events (no per-item ticks
// to feed the TimeEstimator), so the tqdm-style n/total % [elapsed, ETA,
// rate] line would be pure noise. Render them as a bare spinner+title
// while running and a checkmark+title once concluded.
const BARE_STAGES = new Set(["chunk"])

export default function AgentTodos() {
  const todos = props.todos || []
  const stages = props.stages || {}
  const ingestionRunning = props.ingestion_running || false
  // ``active`` is the authoritative portal signal: true while the run is
  // in flight (todos non-empty OR ingestion still running), false once
  // everything has concluded. ``agent_todos.js`` portals the card into
  // ``#message-composer`` while active and restores it to its in-chat-flow
  // position when inactive so the concluded stages remain as a
  // chronological record.
  const active = props.active === true
  const lang = props.lang === "en" ? "en" : "de"

  const stageEntries = Object.entries(stages)
  const hasTodos = todos.length > 0
  // Keep concluded stages visible after the run finishes so the block
  // persists as a chronological record of what happened; only hide the
  // section when there are no stages at all.
  const hasStages = stageEntries.length > 0

  // Render a stable, always-present container instead of ``return null``.
  // Returning null from a Chainlit CustomElement leaves the wrapper
  // container empty; when props later transition from content -> empty
  // (e.g. the finally block in on_message clears todos), React removes
  // the card's DOM nodes, and Chainlit's own teardown of the wrapper can
  // then hit ``Node.removeChild: The node to be removed is not a child
  // of this node`` because the children it expects are already gone.
  // An empty, hidden card keeps a stable DOM node for Chainlit to
  // manage across content/empty transitions.
  if (!hasTodos && !hasStages) {
    return (
      <div id="agent-todos-card" data-active="false" style={{ display: "none" }} />
    )
  }

  return (
    <Card className="w-full border-border" id="agent-todos-card" data-active={active ? "true" : "false"}>
      <CardContent className="p-3 space-y-2 text-xs">
        {hasTodos && (
          <div>
            <div className="flex items-center gap-1.5 mb-1.5 text-muted-foreground font-medium">
              <ListTodo className="h-3.5 w-3.5" />
              <span>{lang === "en" ? "Plan" : "Plan"}</span>
            </div>
            <ul className="space-y-0.5">
              {todos.map((todo, i) => {
                const Icon = STATUS_ICONS[todo.status] || Circle
                const cls = STATUS_CLASSES[todo.status] || ""
                return (
                  <li key={i} className="flex items-start gap-1.5">
                    <Icon className={`h-3.5 w-3.5 mt-0.5 shrink-0 ${cls}`} />
                    <span className={todo.status === "completed" ? "line-through text-muted-foreground" : ""}>
                      {todo.content}
                    </span>
                  </li>
                )
              })}
            </ul>
          </div>
        )}

        {hasTodos && hasStages && <hr className="border-border" />}

        {hasStages && (
          <div>
            <div className="flex items-center gap-1.5 mb-1.5 text-muted-foreground font-medium">
              <Gauge className="h-3.5 w-3.5" />
              <span>{lang === "en" ? "Progress" : "Fortschritt"}</span>
            </div>
            <ul className="space-y-0.5 font-mono text-[11px]">
              {stageEntries.map(([stage, s]) => {
                const done = s.done === true
                const bare = BARE_STAGES.has(stage)
                const Icon = done ? CheckCircle2 : LoaderCircle
                const iconCls = done
                  ? "text-emerald-600"
                  : "text-amber-500 animate-spin"
                return (
                  <li key={stage} className="flex items-center gap-1.5">
                    <Icon className={`h-3 w-3 shrink-0 ${iconCls}`} />
                    {bare ? (
                      <span>{s.title || stage}</span>
                    ) : (
                      <span>
                        {s.title || stage}
                        {"  —  "}
                        {s.total > 0 ? `${s.n}/${s.total}` : `${s.n}`}
                        {" "}
                        {s.total > 0 ? formatPercent(s.percent) : "?"}
                        {"  ["}
                        {s.elapsed_str || "?"}
                        {" elapsed, ETA "}
                        {s.eta_str || "?"}
                        {", "}
                        {formatRate(s.rate)}
                        {"]"}
                      </span>
                    )}
                  </li>
                )
              })}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
