# Design brief

## 1. Product purpose
Photo Curator groups similar local photos so people can choose the best frame and organize the rest without risking their originals.

## 2. Primary user
A person sorting a personal photo library on their own computer, using a large screen for deliberate comparisons and a phone or tablet for quick review.

## 3. Principles
1. **The photograph earns the focus.** Keep interface chrome quiet and give images enough room to compare.
2. **A choice should feel deliberate.** Keep the selected frame, next action, and consequences clear before anything is moved.
3. **Local means local.** Make privacy visible and never imply that originals have moved before confirmation.
4. **One system, every screen.** Shared spacing, type, color, controls, and responsive behavior should feel related across the workflow.

## 4. Success metric
People can compare a group, identify the frame they want to keep, and understand the next safe action without hunting through the interface.

## 5. Out of scope
- Learned photo analysis and ranking are in scope: recommendations may use confirmed choices, but remain suggestions until the user confirms them.
- Useful, truthful photo-comparison signals may be shown during main-page selection; unavailable measurements must not be presented as facts.
- Does not move or delete originals during review.
- Does not replace the existing keyboard shortcuts or review workflow.

## 6. Learned constraints
- **2026-10-18** — Avoid near-black canvases, loud accent fills, cramped controls, and arbitrary spacing. *Why:* the interface should feel like a considered photo workspace, not a generic dark dashboard.*
- **2026-10-18** — Keep the interface overwhelmingly monochrome, using muted color only for meaningful states such as progress, success, rejection, and errors. *Why:* the photographs should carry the color while restrained status cues add clarity and a little life without diluting the premium grayscale identity.*
- **2026-10-18** — Make primary actions immediately recognizable through plain-language labels, visible icons, clear hierarchy, and responsive hover, focus, and pressed feedback. *Why:* choosing, confirming, and removing are the most consequential interactions after comparing the photos.*
- **2026-10-18** — Keep organization confirmation focused on the naming rule and one prominent filename example; do not show a row-by-row file rename table. *Why:* people need to understand the result at a glance, not inspect a long list of repetitive changes.*
- **2026-10-19** — Keep the confirmation page as the complete, concise pre-action recap; keep completion to a short success message and one restart action, without repeating metrics. *Why:* people should understand the consequences before confirming, then finish without redundant information.*
- **2026-10-18** — Make intake feel like the beginning of a photo-curation journey, with a distinctive, reassuring folder-selection state and visible confirmation of the chosen location. *Why:* the first screen should establish product identity and help people feel confident about which local photos they are about to review.*
- **2026-10-18** — Keep the intake and analysis steps full-canvas, compact, and visible together whenever the viewport allows; do not place them inside a raised card. *Why:* the opening flow should feel immediate and uncluttered, with the important choice or progress visible without unnecessary scrolling.*
