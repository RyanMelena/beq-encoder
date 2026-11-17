# How to set up a UI hill climb

A UI hill climb changes the UI in rounds and keeps a round only when judges score it higher.
The first climb scored each screen from its own screenshot.
That rubric rewarded tidy screens, and it did not measure whether the path through the app made sense.
Follow these steps to climb toward the [UX guidelines](ux-guidelines.md) instead.

1. Agree on the target flow before the first round.
   Give every refactor agent the UX guidelines as the design to build.
   Do not let an agent only answer the judges' complaints about one screen.

2. Split the work by flow.
   One agent owns the whole path, from the Library row through the film detail page and the wizard to the finished BEQ track.
   If you split the work by page, each agent polishes its page and the page structure never changes.

3. Capture journeys.
   Capture the screens of a task as one ordered sequence, so that the judges see every click.
   Use these tasks:
   - See the filters for a film.
   - Add a BEQ track with the defaults.
   - Design a filter with BEQ Forge and add a BEQ track from it.
   - Make a clip that compares two filters, and then add one of them to the film.
   - Recover from a failed encode.

4. Score the journey.
   Add these measures to the rubric:
   - The number of clicks from the Library to see a film's filters. The target is one, which opens the film detail page.
   - The number of clicks from the film detail page to add a BEQ track with the defaults. The target is three.
   - Whether the next step is obvious on every screen.
   - Whether any step asks for a decision before it shows the facts for that decision.

5. Seed complete data.
   Give the seed level-change estimates, job histories, and a library folder that exists in the container.
   Without them, the judges score the missing data.
