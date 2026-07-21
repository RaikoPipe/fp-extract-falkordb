MASCHINENPARAMETER – INDUSTRIEWÄSCHEREI NEUBAU
Tabellarische Zusammenstellung | Bearbeiter: M.M. | Letzte Änderung: handschriftlich ergänzt
================================================================================
HINWEIS: Diese Tabelle ist aus mehreren Quellen zusammengeführt worden.
Spaltenbezeichnungen variieren je Abschnitt. Fehlende Werte = "k.A." oder leer.
================================================================================


------------------------------------------------------------------------
ABSCHNITT A: WARENEINGANG & VORVERARBEITUNG
------------------------------------------------------------------------

Agregat-ID  | Bezeichnung            | Taktzeit        | Kapazität       | Verteilung         | Bem.
------------|------------------------|-----------------|-----------------|--------------------|-----------------
WE-01       | Eingangspuffer         | –               | unbegrenzt      | –                  | FIFO; Einheit: Stück
WE-02       | Sortierung/Losbildung  | 1,5 s / Stück   | 1 Los parallel  | –                  | Losbildungszeit = n_Stück × 1,5 s


LOSPARAMETER (aktuell geplant):

  Typ          | Losgröße [Stk.] | Lose/Zyklus | Losgewicht max. [kg]  | Folge-Nr.
  -------------|-----------------|-------------|------------------------|----------
  Hemd         | 223             | 6           | 49,95                 | 1
  Handtuch     | 69              | 6           | 49,75                 | 2
  Tischtuch    | 51              | 6           | 49,73                 | 3
  Bettlaken    | 86              | 6           | 49,97                 | 4

  >> Losgewicht berechnet auf Basis Stückgewichte aus Artikelspezifikation.
  >> Hemd: 223 × 0,224 kg = 49,95 kg  ✓ (unter 50 kg Limit)
  >> Handtuch: 69 × 0,721 kg = 49,749 kg ✓
  >> Tischtuch: 51 × 0,975 kg = 49,725 kg ✓
  >> Bettlaken: 86 × 0,581 kg = 49,966 kg ✓


------------------------------------------------------------------------
ABSCHNITT B: HAUPTPROZESSE – WASCHEN
------------------------------------------------------------------------

Maschinen-Nr.  | Maschinentyp                | Kammern  | Max. Last/Kammer | Prozesszeit/Kammer | Ges.-Prozesszeit
---------------|----------------------------|----------|------------------|---------------------|------------------
TWA-01         | Tunnelwaschanlage (Batch)  | 6        | 50 kg            | 3 min               | 18 min

Weitere Parameter TWA-01:
  - Simultane Nutzung: bis zu 6 Lose gleichzeitig in Kammern
  - Kammer 1 kann neu beladen werden, sobald Lot in Kammer 2 übergeht
  - Belade-/Entladezeit: vernachlässigt
  - Transferzeit zwischen Kammern: in Verweilzeit enthalten
  - Umrüstbedarf bei Wäschetypwechsel: JA
  
  Umrüstzeit TWA-01:
    Eingetragen (Schichtleiter-Angabe):   7 min  [Fixwert]
    Laut Lastenheft v1.1:                 5–10 min  [Gleichverteilt]
    !! Diskrepanz – für Simulation zu entscheiden !!


------------------------------------------------------------------------
ABSCHNITT C: NACHVERARBEITUNG (sortiert nach Wäschetyp)
------------------------------------------------------------------------

>> C.1 TISCHTÜCHER & BETTLAKEN

Maschinen-Bezeichnung  | Kurzname  | Zykluszeit        | Parallelität | Einheit    | Quelle
-----------------------|-----------|-------------------|--------------|------------|----------------
Mangelstraße + Falter  | MNG-01    | 28 s/Stück        | 2 Stück      | s/Stück    | Herstellerangebot v2.3
                       |           | (alt: 30 s/Stück) |              |            | ältere Planung

Nachgelagerte Puffer: vorhanden (FIFO, unbegrenzt)
Wäschetypen: Bettlaken + Tischtücher (gemeinsam)


>> C.2 HEMDEN

Masch.-ID  | Bezeichnung         | Kennwert 1              | Kennwert 2                    | Kennwert 3
-----------|---------------------|-------------------------|-------------------------------|---------------------------
FIN-01     | Finisher (Tunnel)   | v = 0,2 m/s             | Tunnellänge: k.A.             | Mindestabstand: 3 cm/Hemd
FST-H-01   | Falten & Stapeln    | Stapelgröße: 10 Stück   | μ = 10 s / Stapel             | σ = 0,5 s / Stapel

  Hinweis FST-H-01: Letzte Besprechung (Protokoll vom [Datum fehlt]) nannte
  Stapelgröße = 12 Stück. Bitte mit Betrieb klären. Aktuell: 10 Stück angenommen.

  Hinweis FIN-01: Tunnellänge nicht eingetragen. Aus Geschwindigkeit und Mindestabstand
  ergibt sich max. Bestand im Tunnel = Tunnellänge / (Hemdbreite + Abstand).
  Tunnellänge aus Lieferantenangebot nachzutragen!


>> C.3 HANDTÜCHER

Aggregate       | Parameter                          | Wert               | Einheit        | Anmerkung
----------------|------------------------------------|--------------------|----------------|----------------------------
TRO-01          | Trockner – Kapazität               | 60                 | kg             | –
TRO-01          | Trockner – Programmdauer           | 20                 | min            | Fixwert (Annahme Planung)
TRO-01          | Anzahl Trockner                    | k.A.               | –              | noch nicht festgelegt
FST-T-01        | Falten & Stapeln – Stapelgröße     | 5                  | Stück          | –
FST-T-01        | Falten & Stapeln – Bearbeitungszeit | 8 (μ), 0,25 (σ)  | s / Stapel     | Normalverteilt

  Wichtiger Hinweis TRO-01: Laut Aufgabenstellung ist die Programmdauer DREIECKSVERTEILT
  (15 min / 20 min / 30 min). Vorstehend ist nur der Modalwert 20 min eingetragen.
  Für die Simulation ist die vollständige Verteilung zu verwenden!
  [Randnotiz handschriftlich: "Energieverbrauch bei 30-min-Programm prüfen – Betrieb"]


------------------------------------------------------------------------
ABSCHNITT D: PUFFER (ÜBERSICHT)
------------------------------------------------------------------------

Position                        | Kapazität    | Strategie | Typ
--------------------------------|--------------|-----------|-------------------------
Nach Wareneingang (vor Sort.)   | unbegrenzt   | FIFO      | allg. Puffer
Nach Sortierung (vor Waschen)   | unbegrenzt   | FIFO      | allg. Puffer
Nach Waschen – Tischtuch/Bett   | unbegrenzt   | FIFO      | allg. Puffer
Nach Waschen – Hemden           | unbegrenzt   | FIFO      | allg. Puffer
Nach Waschen – Handtücher       | unbegrenzt   | FIFO      | allg. Puffer
Nach Finisher (vor F&S Hemden)  | unbegrenzt   | FIFO      | allg. Puffer
Nach Trockner (vor F&S Handtuch)| unbegrenzt   | FIFO      | allg. Puffer


------------------------------------------------------------------------
QUERVERWEISE & ÄNDERUNGSHISTORIE
------------------------------------------------------------------------

Datum       | Änderung                                             | Bearbeiter
------------|------------------------------------------------------|------------
[unbekannt] | Erstanlage Tabelle (Abschnitt A+B)                   | k.A.
[unbekannt] | Abschnitt C ergänzt aus Lieferantenangebot           | M.M.
[unbekannt] | Handschriftliche Ergänzung: Umrüstzeit-Diskrepanz    | [unleserlich]
[TBD]       | Tunnellänge Finisher nachtragen                      | Einkauf
[TBD]       | Kammeranzahl TWA-01 bestätigen (s. Lastenheft #7)    | Lieferant
