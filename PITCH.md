# CollegeNav pitch kit

## 30-second version
Every morning thousands of students guess which bus to take, from where, and when. The timetable on the notice board is months old, and when a bus is late nobody knows. CollegeNav is one mobile page: type where you are and where you are going, and it shows the right route, the nearest stop, the next buses, and how far the bus is right now. Transport staff update routes and publish delays in seconds, and every piece of information shows how recently it was verified, so students always know what to trust.

## 8 slides (exact text)
1. **Hook.** "Ananya missed her 8:00 AM lab because the bus she waited for stopped running three weeks ago. Nobody told her." Subtitle: CollegeNav, intelligent college bus navigation.
2. **Problem.** Students can't tell which route serves them, where the nearest stop is, or if a route changed today. Schedules are outdated and spread over WhatsApp groups and paper notices. Transport staff have no single place to publish changes.
3. **Solution.** One mobile page for students and one control panel for admins. Plan a trip, see the stops and times, watch the bus, get notified. (Screenshot of the home screen.)
4. **Live demo.** (Switch to the app. Script below.)
5. **How it works.** Admin enters stops, order, minutes between stops and timetable. Search matches stops to routes and ranks by next departure, with one-change journeys. The driver's phone shares GPS; the bus position is projected onto the route to give an ETA per stop. If GPS is lost, the app says so and falls back to the timetable. (Simple architecture diagram: Student phone <-> Flask API <-> SQLite; Driver phone -> GPS -> API; Admin -> API.)
6. **Impact (estimates).** A college with N bus routes and a few thousand riders. Even 5 minutes saved per rider per day is hundreds of hours a week across the campus (estimate: riders x 5 min x working days). Fewer missed classes, fewer calls to the transport office, fewer unsafe waits at night.
7. **Scale and sustainability.** Needs no hardware: the driver's phone is the tracker, free maps, free hosting. Works for any college or school with buses. Roadmap: QR codes at stops, regional-language support, delay prediction from past trips, integration with attendance.
8. **Team and ask.** Name, role. Ask: a pilot with the college transport office on two routes for one month.

## Demo script (3 minutes)
1. (0:00) Open the home page on a phone-sized window. "I am a new student at Hayathnagar. I don't know the buses." Tap the location button or type the stop. Search to College Main Gate. Show next buses, minutes to wait, "bus is about N min away", and the verified badge.
2. (0:45) Open a route: map, stop order, ETAs for each stop. Point at the "verified 40 days ago, may be outdated" route: "We never hide uncertainty."
3. (1:15) Search a suspended route: it is flagged unavailable. Search Uppal to Abdullapurmet: show the one-change suggestion.
4. (1:45) Open /admin in a second window. Publish a notice: "R1 is delayed 20 minutes". Flip to the student window: a pop-up appears within seconds, and the route card shows the delay.
5. (2:15) Live tracking: open /driver on your phone, start sharing, and show the bus marker moving on the live map. "No GPS hardware needed." If the venue has no internet or GPS, say "this is the simulator, clearly labelled", and show the labelled demo mode.
6. (2:45) Close: "Everything you saw is editable by the transport office, no developer needed."

Backup if the internet or deploy fails: run on localhost, or play the recorded screen video. Keep demo logins ready.

## Judge Q&A
1. **How is this different from Google Maps?** Maps doesn't know private college buses, their stops or timings, and can't carry a notice from the transport office. We are the college's own source of truth, with a freshness label on every route.
2. **Where do the live locations come from?** The driver's phone, through a simple page with a PIN. No hardware to buy. Where no GPS exists we show the timetable and say live data is unavailable.
3. **How accurate are the ETAs?** ETAs use the position along the route and the planned minutes between stops. They are not traffic-aware yet. We can improve them by learning from past trips.
4. **What if the data is wrong or old?** Every route shows when it was last verified; after 14 days it turns amber and after 30 days red. Admins see a health dashboard of stale routes.
5. **Who maintains it?** Transport staff, through a simple admin panel: stops, minutes, times, notices. No coding needed.
6. **How do students get notified?** In-app pop-ups within seconds, and browser notifications if they allow them. A WhatsApp or SMS gateway is the next step.
7. **What about privacy?** Students need no account and we collect no personal data. Only the bus location is shared, never a student's. (The "use my location" feature runs in the browser and is not stored.)
8. **How does it scale?** One lightweight server and a database. Moving to a hosted database and caching handles thousands of riders; another college is just another set of routes.
9. **What if a driver forgets to start GPS?** The app falls back to the timetable and shows "live location unavailable". Admins see which buses are sending GPS.
10. **What would you build next?** QR codes at every stop, regional languages, delay prediction, offline timetable, and attendance integration.

## Honest notes (say them before the judges find them)
- Demo routes are sample data; the simulator is labelled "Simulated (demo data)".
- Real GPS works through the driver page; ETA accuracy depends on the timetable offsets entered.
- Impact numbers are estimates; replace them with the real rider count if you can get it.
