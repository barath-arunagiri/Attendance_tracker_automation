# Attendance Automation (WhatsApp group -> Login & Leave Tracker)

## Build the .exe (once, on a Windows laptop)
1. Install Python 3.10+ from python.org (tick "Add Python to PATH"). Do not build inside OneDrive/DataPattern; use e.g. `C:\AttendanceAutomation`.
2. Double-click `build.bat`. It closes any running old copy, downloads a portable Node.js if needed, and builds.
3. Run `dist\AttendanceAutomation\AttendanceAutomation.exe`. Keep that whole folder together.
   Your old settings, linked employees and WhatsApp link (from `AttendanceAgent`) are copied automatically the first time - no new QR scan needed.
4. In **3. Settings**, enable **Start automatically when Windows starts, unlocks, or wakes**, then click **Save settings**. Windows starts the app at sign-in and relaunches it when you unlock or wake the laptop if you had closed it. Disable the option and save to turn automatic startup off.

## First run
1. **Settings**: Browse to the synced tracker .xlsx, set the CL time, leave **Test mode ON**, Save.
2. **1. Connect**: scan the QR (WhatsApp > Linked devices) if asked.
3. **2. Group & employees**: pick the group, then use "People who posted in the group" for manual linking or "Show tracker employees only" to list only group members whose saved link/name matches a tracker employee. Unmatched group participants are hidden from that view. Set Timing and Save links.
4. Watch the **Messages** tab for attendance messages from the selected group. It shows the date, sender, attendance type, and tracker-link status. Both linked and unlinked senders are shown; only linked check-ins can update the tracker. To name an unlinked sender whose WhatsApp contact name is unavailable, select one of their messages and choose **Set display name for selected sender**; this does not link them to an employee or change tracker behavior. Logout messages are display-only. Entries are saved locally from the time this version is run, so they remain visible after a restart. Watch the **Log** tab for a day (test mode shows what *would* be written). Untick Test mode and Save settings if automatic tracker updates are intended. Test mode is on by default and never writes to the tracker.

At Windows sign-in, session unlock, or laptop resume from sleep, the app starts (or remains running if already open), reconnects to the saved WhatsApp session, and watches the selected group. Independently of WhatsApp connection, it checks the tracker at startup and once per day: if the current month's tab is missing, it creates one by copying the latest earlier monthly layout, setting weekday dates from the real calendar, and adding linked employee names/timings. On the last calendar day of the month it also prepares the next month's tab. This means opening the laptop after month-end still creates the current month's missing tab. Daily catch-up scans blank days for today and the configured lookback and fills from linked check-ins already saved locally; it does not request WhatsApp history or require the phone to be online. While connected, new group messages are processed and linked check-ins are cached locally for future catch-up. Use **Read earlier messages now** or select the history option in **Fill empty days** only when older messages are missing from the local cache; those one-time history requests need the phone online. A setting of **0** checks today and yesterday. This requires **Test mode OFF** for actual tracker writes; the default is ON as a safety check.

## Fill empty days (new)
**Messages tab > Fill empty days...**
1. Choose the date range (default: 1st of this month to today).
2. Tick *Ask WhatsApp for earlier messages first* (phone online). In this dialog, the history request uses the date range you selected and stops at its oldest blank tracker day. The separate **Read earlier messages now** button uses **Days to fill from WhatsApp history** as its lookback. WhatsApp allows at most 50 messages per history request, so the helper continues paging to the target date (or until WhatsApp has no older messages). Optionally add an exported chat file for older days
   (WhatsApp > group > More > Export chat > Without media).
3. The app scans the tracker for weekdays where **Reported and Status are blank** (for linked employees), collects every check-in it can find
   (WhatsApp history, check-ins saved by the app, the chat file) and shows a **preview**: time + On-time/Late, "-> CL", or "left empty".
4. **Apply** writes it. Hand-filled cells, Holiday/Weekend/Leave and today-before-CL-time are never touched. Running it twice changes nothing.
   Weekdays that are over with no check-in become CL (untick the option to leave them blank instead).
   Automatic catch-up checks blank tracker days within **Days to fill from WhatsApp history** (0 = today and yesterday), using locally saved check-ins only; it never requests WhatsApp history automatically. **Fill empty days** remains limited to the date range selected in its dialog and requests history only if you tick the history option and there are blank tracker entries in that range. WhatsApp history is fetched in pages of up to 50 messages.
   When a new month tab is created, it copies the previous month layout but clears old fills from blank Reported and Status cells. Startup also clears inherited fills from blank attendance cells in an existing current-month tab while leaving filled statuses untouched. Empty entries therefore stay white instead of carrying old green/red status colors.
   Status cells copy the most common existing fill and alignment for the same status on that month tab, so On-time/Late match the workbook's formatting. CL formatting is taken from the current month's tab, including when writing to another month; if that tab has no CL example, the peach fallback is used. Reported login times use the common alignment from their week block.

Tracker writes and new-month tabs are saved through desktop Excel to the workbook selected in Settings, changing only the affected cells or adding the new tab. This avoids replacing the synced workbook file behind Excel. Excel must be installed; if it opens the tracker read-only or reports a save conflict, the app logs an error and does not fall back to replacing the file. A `written and verified in <filename>` log confirms values were read back from the saved workbook; it cannot confirm that OneDrive/SharePoint has finished syncing to the online workbook. Automatic fill retries tracker write/read-back errors up to three times total and logs each failure. Select the locally synced copy of the exact SharePoint workbook (not a downloaded or separate copy), and check its sync status if the online sheet does not change.

The WhatsApp QR refreshes while it is waiting to be linked. Scan the latest QR shown in the Connect tab; it stops refreshing once WhatsApp connects. A QR is not needed when the saved WhatsApp session connects successfully.
If the QR does not appear, read the status above the QR area and the Log tab. After 30 seconds without a QR or connection, the Connect page reports a timeout. The helper uses the current version advertised by WhatsApp Web, with a real five-second request timeout and a bundled-version fallback. It loads the saved login and checks the web version once per helper run, reusing both on reconnects; retry delays are 2, 4, then 8 seconds. It uses Baileys 7.0.0-rc14 for the current WhatsApp Web protocol. Every build synchronizes the locked Node dependencies before packaging, so an existing `node_modules` folder cannot leave an old WhatsApp library in the app. The Log includes the selected WhatsApp Web version, disconnect code/reason, and WebSocket handshake status/close reason (not chat contents). **Restart WhatsApp helper** restarts the connection without removing saved login; **Re-link WhatsApp** removes that login and requests a fresh QR. Repeated connection failures stop after three retries instead of looping indefinitely.

## What was fixed in this version
- **"WhatsApp did not send earlier messages" / "Nothing to start from yet"**: the helper was set up so the WhatsApp library silently threw away every
  history reply. It now accepts them, shows progress ("Received N earlier messages..."), waits by itself for a starting message instead of
  failing, and ignores repeated clicks (the log flood).
- People who appear under a hidden WhatsApp id on some messages and a phone number on others are now recognised as one person.
- Check-ins of linked employees are saved locally (`checkins.json`: time, name, message text; nobody else, no other messages) so they survive restarts.
- Attendance messages from the selected group are saved locally in `attendance_messages.json` (up to 4,000 entries), including unlinked senders. The Messages tab separates login, reached-office, and logout entries by date and shows current tracker-link status. Logout entries are display-only.
- Only one copy can run at a time (two copies fought over the WhatsApp link).
- Tracker reads and writes run in a serialized background worker, so workbook scans and saves do not freeze the tabs. Large message batches and member lists are rendered in bounded chunks.
- Renamed to AttendanceAutomation (exe, window, build script, data folder).

## Limits
- Never run against live WhatsApp or your real tracker by me - use Test mode first. Reading older messages needs your phone online;
  if WhatsApp still sends nothing, use the exported chat file (always works).
- The current month's missing tab is created from the previous monthly layout, and the next month's tab is prepared on the final calendar day. Older missing tabs in a requested history range are not generated automatically; the log identifies those skipped months.
- Unofficial WhatsApp Web link: read-only use is low risk, but a restriction is possible. Open the app at least every ~2 weeks.
- Excel edits to the workbook on this laptop are made through Excel, which helps preserve coauthoring and OneDrive sync behavior. Concurrent edits from another person can still cause a conflict; resolve any Excel merge prompt before retrying.
- A backup is kept before the first write each day (`%APPDATA%\AttendanceAutomation\backups`).
