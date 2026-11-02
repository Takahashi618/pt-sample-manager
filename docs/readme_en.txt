PT Sample Manager {VERSION}
README

This folder contains 8 files and 1 app. Some file names are in Japanese; the English name follows each.

  PTサンプルマネージャ.app  The app (Finder shows it as PT Sample Manager when your Mac is in English)
  README (English).txt  This document
  Manual (English).html  How to use the app (opens in a browser; you can move by headings)
  Manual (English).txt  The same manual as plain text
  はじめにお読みください.txt  This document in Japanese
  マニュアル.html, マニュアル.txt  The manual in Japanese
  ライセンス.txt  License: the terms of use of this software (MIT License, in English with a Japanese summary)
  同梱部品のライセンス.txt  Third-party licenses: the terms of the parts made by others inside the app

== What it is

An app that finds audio samples from Splice or your own folders, previews them, and places them on
Pro Tools tracks. It can also process a sample to match the session tempo and key before placing it.
It is designed to be used with VoiceOver and the keyboard only. "PT" in the name stands for Pro Tools.
Pro Tools is a product of Avid Technology; this app is not affiliated with Avid Technology.

== Requirements

- A Mac with Apple silicon (M1 or later), macOS 15 or later (tested by the author on 15.7.8)
- Pro Tools 2025.6 or later, for placing (tested by the author on 2026.4.1)
- VoiceOver
- The app's window and speech follow your Mac's language settings: English or Japanese (chapter 2 of the manual)
- Matching tempo and key and changing between mono and stereo need ffmpeg (install it with Homebrew).
  rubberband improves the sound quality

== Installing (short version)

1. Move "PTサンプルマネージャ.app" from this folder to the Applications folder
2. Open it. The first time, macOS may stop it from opening.
   How to open it is in "3.2 Opening the app for the first time" in the manual
3. Press Command-F to add your sample folder, then Command-R to scan

For details, read "Manual (English).html" (or "Manual (English).txt").
Inside the app, Command-Shift-K opens the list of keyboard commands.

== Made with AI

This software was made with AI (Anthropic's Claude) under the direction and decisions
of the author (Takahashi). Most of the program code and of the text in the manual
and other documents was written by the AI.

== License

MIT License (Copyright (c) 2026 Takahashi). It is free to use, with no warranty.
For details, read "ライセンス.txt" (License).
