-- Extract TV.app artwork for a set of tracks.
--
-- Reads a job file of "index<tab>databaseID" lines (1-based indices into the
-- library playlist) and writes <databaseID>.jpg into outDir. Indices are used
-- rather than "whose database ID is ..." because that form rescans the whole
-- 7k-track library on every lookup.
--
-- Writes one progress line per item to a log file so a long background run
-- can be followed from outside.

on run argv
  set jobPath to item 1 of argv
  set outDir to item 2 of argv
  set logPath to item 3 of argv

  set jobText to read (POSIX file jobPath) as «class utf8»
  set oldDelims to AppleScript's text item delimiters
  set AppleScript's text item delimiters to linefeed
  set jobLines to text items of jobText
  set AppleScript's text item delimiters to oldDelims

  set doneCount to 0
  set missingCount to 0
  set failCount to 0
  set logFile to open for access (POSIX file logPath) with write permission
  set eof logFile to 0

  tell application "TV"
    set lib to library playlist 1
    repeat with aLine in jobLines
      if length of aLine > 0 then
        set AppleScript's text item delimiters to tab
        set parts to text items of aLine
        set AppleScript's text item delimiters to oldDelims
        if (count of parts) is 2 then
          set idx to (item 1 of parts) as integer
          set dbid to item 2 of parts
          try
            set t to track idx of lib
            if (count of artworks of t) > 0 then
              set d to raw data of artwork 1 of t
              set p to outDir & "/" & dbid & ".jpg"
              set outFile to open for access (POSIX file p) with write permission
              set eof outFile to 0
              write d to outFile
              close access outFile
              set doneCount to doneCount + 1
              my logLine(logFile, "ok " & dbid)
            else
              set missingCount to missingCount + 1
              my logLine(logFile, "none " & dbid)
            end if
          on error errMsg
            set failCount to failCount + 1
            try
              close access outFile
            end try
            my logLine(logFile, "fail " & dbid & " " & errMsg)
          end try
        end if
      end if
    end repeat
  end tell

  my logLine(logFile, "DONE ok=" & doneCount & " none=" & missingCount & " fail=" & failCount)
  close access logFile
  return "ok=" & doneCount & " none=" & missingCount & " fail=" & failCount
end run

on logLine(f, msg)
  try
    write (msg & linefeed) to f
  end try
end logLine
