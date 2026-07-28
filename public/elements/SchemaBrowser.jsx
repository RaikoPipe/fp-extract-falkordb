import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Boxes, Link2, Key } from "lucide-react"

export default function SchemaBrowser() {
  const labels = props.labels || []
  const rels = props.relationship_types || []
  const keys = props.property_keys || []

  return (
    <Card className="w-full">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium flex items-center gap-2">
          <Boxes className="h-4 w-4" />
          Graph Schema
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        <div className="space-y-1">
          <div className="flex items-center gap-1.5 font-medium text-muted-foreground">
            <Boxes className="h-3.5 w-3.5" />
            Node labels ({labels.length})
          </div>
          <div className="flex flex-wrap gap-1">
            {labels.length === 0 ? (
              <span className="text-muted-foreground">None</span>
            ) : (
              labels.map((l) => (
                <Badge key={l.name} variant="secondary" className="font-mono">
                  {l.name}
                </Badge>
              ))
            )}
          </div>
        </div>

        <div className="space-y-1">
          <div className="flex items-center gap-1.5 font-medium text-muted-foreground">
            <Link2 className="h-3.5 w-3.5" />
            Relationship types ({rels.length})
          </div>
          <div className="flex flex-wrap gap-1">
            {rels.length === 0 ? (
              <span className="text-muted-foreground">None</span>
            ) : (
              rels.map((r) => (
                <Badge key={r.name} variant="outline" className="font-mono">
                  {r.name}
                </Badge>
              ))
            )}
          </div>
        </div>

        <div className="space-y-1">
          <div className="flex items-center gap-1.5 font-medium text-muted-foreground">
            <Key className="h-3.5 w-3.5" />
            Property keys ({keys.length})
          </div>
          <ScrollArea className="h-24 w-full rounded border p-1">
            {keys.length === 0 ? (
              <span className="text-muted-foreground">None</span>
            ) : (
              <div className="flex flex-wrap gap-1">
                {keys.map((k) => (
                  <span
                    key={k}
                    className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]"
                  >
                    {k}
                  </span>
                ))}
              </div>
            )}
          </ScrollArea>
        </div>
      </CardContent>
    </Card>
  )
}