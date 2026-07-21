================================================================
RAUMKONZEPT & FLÄCHENPROGRAMM – NOTIZ
Projekt: Industriewäscherei Neubau
Erstellt von: [Initialen unleserlich]
Besprechungsgrundlage, nicht abgestimmt
Stand: Entwurf 0.1 – NUR ZUR INTERNEN DISKUSSION
================================================================
[Notiz oben rechts auf erstem Blatt, handschriftlich:
"Jonas – hab mal versucht das Layout grob zu skizzieren auf Basis
der Prozessbeschreibung. Zahlen sind geraten wo nix steht!
Bitte nicht weitergeben. – M."]
================================================================


----------------------------------------------------------------
1. PROZESSSTRUKTUR (Grundlage: Konzeptmodell aus Aufgabenstellung)
----------------------------------------------------------------

Aus dem Prozessdiagramm ergibt sich folgender Materialfluss:

  [WE-Puffer]
       ↓
  [Sortierung & Losbildung]
       ↓
  [Puffer vor Wascher]
       ↓
  [Tunnelwaschanlage TWA-01] ← 6 Kammern, 3 min/Kammer
       ↓ (Typ-abhängige Weiche)
       ├─→ [Puffer] → [Mangelstraße MNG-01] → [Ausgang]     ← Bettlaken + Tischtücher
       ├─→ [Puffer] → [Finisher FIN-01] → [Puffer] → [F&S Hemden FST-H-01] → [Ausgang]
       └─→ [Puffer] → [Trockner TRO-01] → [Puffer] → [F&S HT FST-T-01] → [Ausgang]

  Alle drei Ausgänge führen in gemeinsamen Versandbereich (⊗ im Diagramm)

Anmerkung: Diese Struktur entspricht dem Konzeptmodell (Abbildung 1
der Aufgabenstellung). Layoutvarianten sind noch nicht bewertet.


----------------------------------------------------------------
2. FLÄCHENPROGRAMM (ERSTER ENTWURF)
----------------------------------------------------------------

HINWEIS: Flächen mit "*" sind ANNAHMEN mangels Herstellerangaben.
         Flächen mit "**" sind aus Maschinenparametern abgeleitet.
         Flächen mit "TBD" sind vollständig ungeklärt.

Zone / Aggregat                | Bezeichnung          | Fläche [m²]  | Quelle/Basis
-------------------------------|----------------------|--------------|---------------------------
Z-01  Wareneingang & WE-Puffer | Eingangspuffer       | TBD          | kein Maßstab vorhanden
Z-02  Sortierung & Losbildung  | Losbildungsstation   | TBD          | Anzahl MA unklar
Z-03  Puffer vor Wascher       | Zwischenpuffer Sort. | TBD          | –
Z-04  Tunnelwascher (TWA-01)   | Hauptwaschen         | TBD*         | Maschinenabm. fehlen
Z-05  Puffer nach Wascher (3×) | Verteilpuffer (3 Stk)| TBD          | –
Z-06  Mangelstraße (MNG-01)    | Mangeln + Falten     | TBD*         | Maschinenabm. fehlen
Z-07  Finisher (FIN-01)        | Tunnelfinisher       | ca. 4 × [B]**| Tunnellänge = 4 m (!)
Z-08  F&S Hemden (FST-H-01)    | Falten & Stapeln HE  | TBD          | –
Z-09  Trockner (TRO-01)        | Trocknerstation      | TBD*         | Anzahl Trockner unklar
Z-10  F&S Handtücher (FST-T-01)| Falten & Stapeln HT  | TBD          | –
Z-11  Versand / Ausgangspuffer | Kommissionierung     | TBD          | –
Z-12  Verkehrsflächen, Wege    | Interne Logistik     | TBD          | Mindestbreite: unklar

  ** Finisher: Tunnellänge 4 m aus Aufgabenstellung bekannt.
     Breite (= [B]) nicht angegeben → Flächenberechnung unvollständig.
     Für Simulation: nur Prozesszeit relevant, nicht Fläche.


----------------------------------------------------------------
3. ANORDNUNGSLOGIK (QUALITATIV)
----------------------------------------------------------------

Prioritäten für die Layoutentwicklung (aus Materialflussstruktur):

P1 – Hauptfluss möglichst geradlinig:
     WE → Sortierung → Wascher → Nachverarbeitung → Versand
     (Vermeidung von Gegenläufigkeiten und Kreuzungen)

P2 – Drei Nachverarbeitungsstränge parallel anordnen:
     Mangelstraße (oben) | Finisher (Mitte) | Trockner (unten)
     entsprechend Konzeptmodell-Anordnung aus Aufgabenstellung

P3 – Puffer als Flächen zwischen Prozessschritten vorsehen:
     Alle Puffer unbegrenzt (FIFO) → flexible Flächenreserve einplanen

P4 – Tunnelwascher als zentrales Verteil-Aggregat:
     Weiche nach Wascher muss alle 3 Nachverarbeitungspfade erreichen
     Positionierung im Raumzentrum oder mit kurzen Verbindungswegen

P5 – Wareneingang und Versand möglichst an Außenwänden/Toren:
     LKW-Andockmöglichkeit (2 LKWs/Tag, Ankunft stochastisch)


----------------------------------------------------------------
4. OFFENE FRAGEN LAYOUT
----------------------------------------------------------------

Nr.  | Frage                                              | Relevanz Simulation
-----|----------------------------------------------------|-----------------------
L-01 | Maschinenabmessungen TWA-01 (fehlen komplett)     | nein (nur Prozesszeit)
L-02 | Maschinenabmessungen MNG-01 (s. Lieferantenangebot)| nein
L-03 | Anzahl Trockner TRO-01 (1 oder 2?)                | JA – Kapazität relevant
L-04 | Breite Finisher-Tunnel FIN-01                     | nein (Länge = 4 m bekannt)
L-05 | Flächenbedarf Losbildung (1 Station, 1 Los parallel)| nein
L-06 | Wegbreiten für Wäschewagen / Transporte            | nein (nicht modelliert)
L-07 | Anordnung Puffer: Regal vs. Bodenablage           | nein (Kapazität unbegrenzt)
L-08 | Lage Wareneingang relativ zu Sortierung            | nein (kein Transportmodell)

  → Für Materialflusssimulation (Ist-Zustand) sind Layout-Fragen
    WEITGEHEND IRRELEVANT – entscheidend sind Prozesszeiten, Mengen,
    Stochastik und Steuerungslogik (Lossequenz, FIFO-Puffer, Weiche).

  → Ausnahme: Anzahl Trockner (L-03) direkt kapazitätsrelevant!
    Kapazitätsrechnung (DOC7) deutet auf möglicherweise 2 Trockner hin.


----------------------------------------------------------------
5. SKIZZE GRUNDRISS (textuelle Beschreibung der Handskizze)
----------------------------------------------------------------

[Skizze auf Rückseite des Blattes – hier textuell wiedergegeben]

  ┌─────────────────────────────────────────────────────────────┐
  │ TOR 1 (LKW-Einfahrt)                        TOR 2 (Versand)│
  │                                                             │
  │  [WE-Puffer] → [Sortierung] → [Puffer] → [TWA-01]          │
  │                                               │             │
  │                             ┌─────────────────┤             │
  │                             ↓         ↓       ↓             │
  │                          [MNG-01] [FIN-01] [TRO-01]         │
  │                             ↓         ↓       ↓             │
  │                             └─────────┴───────┘             │
  │                                       ↓                     │
  │                               [Versandpuffer]               │
  └─────────────────────────────────────────────────────────────┘

  [Handschriftliche Randbemerkung: "Maße komplett frei erfunden –
   das ist nur die Prozessstruktur, kein echtes Layout!!
   Bitte NUR als Gesprächsgrundlage verwenden."]

================================================================
Nächster Schritt: Maschinenabmessungen bei Lieferanten anfragen,
dann Varianten-Layout mit Maßstab entwickeln.
Für Simulation: Layoutfragen zurückstellen, Prozessparameter priorisieren.
================================================================
