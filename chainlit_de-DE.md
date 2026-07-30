# FalkorDB Wissensgraph-Assistent

Laden Sie Ihre Planungs-Dokumente hoch und stellen Sie Fragen in natürlicher Sprache – der Assistent baut daraus automatisch einen Wissensgraph auf und beantwortet Ihre Fragen.

## So geht's

1. **Wissensgraph wählen** – öffnen Sie die Seitenleiste und wählen Sie den Graph, den Sie verwenden möchten.
2. **Dateien hochladen** – klicken Sie auf die Büroklammer neben dem Eingabefeld und laden Sie Ihre Dokumente hoch (PDF, Word, PowerPoint, Excel, Bilder, Text, CSV, JSON, HTML).
3. **Dokumente ingestieren** – klicken Sie auf den Button **Dokumente ingestieren**. Der Assistent liest die Dateien, erkennt Inhalte und trägt sie in den Wissensgraph ein.
4. **Fragen stellen** – tippen Sie Ihre Frage in den Chat ein oder nutzen Sie die vorgeschlagenen Beispielfragen.

## Unterstützte Dateitypen

PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx), CSV, JSON, HTML, Markdown, reiner Text, Bilder.

## Was können Sie fragen?

- „Welche Maschinen gibt es und welche Bearbeitungszeiten haben sie?“
- „Zeige mir die Transportrouten und Fahrzeuge.“
- „Welche Schichtmodelle und Mitarbeiterpools sind hinterlegt?“
- „Suche nach Ressourcen, die mit Waschmaschinen zu tun haben.“
- „Wie sieht der Aufbau des Wissensgraphen aus?“

## Tipps

- Laden Sie zuerst die Dateien hoch und klicken Sie dann auf **Dokumente ingestieren** – das reicht für den automatischen Aufbau.
- Wechseln Sie den Wissensgraphen jederzeit über die Seitenleiste.
- Wenn Sie sich unsicher sind, was enthalten ist, fragen Sie einfach: „Was ist im Wissensgraph enthalten?“

## Konten & Chat-Verlauf

- **Anmeldung erforderlich.** Beim ersten Besuch melden Sie sich mit Benutzername und Passwort an.
- **Noch kein Konto?** Öffnen Sie die Seite `/register` (z. B. `http://localhost:8000/register`), um eines anzulegen – wählen Sie einen Benutzernamen, einen optionalen Anzeigenamen und ein Passwort (mindestens 8 Zeichen). Die Registrierung kann durch die Administration deaktiviert werden (`REGISTER_ENABLED=0`).
- **Ihre Chats werden gespeichert.** Jede Unterhaltung wird unter Ihrem Konto abgelegt und in der Seitenleiste aufgelistet; Sie können jeden vergangenen Thread fortsetzen. Hochgeladene Dateien dieser Threads werden auf dem Server unter `./data/elements` vorgehalten.