# Dylan's to-do: hands-on testing

Things only you can check: on your own devices, with real projects, or by
waiting for a scheduled event. Tick them off as you go and note anything
that felt wrong; we'll go through the notes together.

## Project groups (phases 1-2)
- [ ] On a project's Overview, press **New child project** and create one; it
      should show "Part of …" and appear nested under the parent everywhere.
- [ ] Move an existing project into a group from its Settings → **Part of**,
      then remove it again from the parent's Settings.
- [ ] Give the parent a goal that needs work in both (e.g. an API change plus
      the app screen that uses it): one job per project, the app's job waits
      for the API's to be merged, and each is approved on its own.

## Internet access (checkpoint 4)
- [ ] Project Settings → **Internet access**: the two choices read clearly.
- [ ] When a project's tests need the internet, a **Wants internet** card shows
      up under Needs you (and on Discord). Try **Allow for this change** once.
- [ ] A build still downloads its packages (it has internet, just not your
      network or this server).

## Goal assistant (checkpoint 3, deployed 2026-10-01)
- [ ] On a real project, type a rough idea and press **Plan it with me**.
      Are the questions useful, and is the brief right, too long or too short?
- [ ] Try **Skip questions**, **Change something** and editing the brief by hand.
- [ ] Use it from the dashboard home with a project picked in the picker.
- [ ] Reload the page while it is thinking: the conversation should come back.
- [ ] **Send as written** still submits the text unchanged.

## Project types and builds (checkpoint 2)
- [ ] Each project's Overview shows the right type. Open **Why?** and check
      the reasons make sense.
- [ ] On a project SID doesn't recognise, use Settings → **Describe it**,
      then try **Recheck now**.
- [ ] The goal-idea buttons fill the goal box with the first blank selected.
- [ ] Build a real project, download the zip on your PC and run what's inside
      (e.g. a `.love` file in LÖVE, a web build in a browser).
- [ ] A Godot game needs an export preset (Project → Export in Godot) before
      it can build; try one if you have a Godot project.

## Notifications and digest (checkpoint 1)
- [ ] Settings → **Send test**: it arrives on Discord and pings you.
- [ ] Turn on quiet hours and check normal events wait until they end, while
      urgent ones (backup failed, health red) still come through.
- [ ] The first weekly digest arrives **Sunday 2026-10-04 at 18:00**. Is it
      readable, and is anything missing?
- [ ] The Activity tab of a busy project tells the story of what happened.

## Earlier features worth a real-device pass
- [ ] Use the whole dashboard on your phone and tablet (not just the browser
      window at those sizes): home, a project page, Files, Builds, Settings.
- [ ] File browser on touch: long-press menu, select, cut/copy/paste, drag
      onto a folder, upload from the phone.
- [ ] Files on desktop: drag files in from your PC, download a folder as a zip,
      rename with F2, resolve a name clash (overwrite / skip / keep both).
- [ ] **Preview** a change that's waiting for approval and open it from your PC.
- [ ] **Undo** a merged change from a project's History tab.
- [ ] Delete a throwaway project, then restore it from the trash on the
      Projects page (it can be restored for 1 day).
- [ ] Your proxy server reaches the dashboard on port 8080, and project apps
      on their ports (8100-8199).
- [ ] The first monthly restore check runs **2026-11-01 04:30**. Its result shows
      under system health, and a failure pings you.
