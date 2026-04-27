# UI/UX Design Philosophy Guide

## 1. Affordances & Signifiers
Good UI teaches itself. Use visual cues — containers, grays, highlight states, hover effects, tooltips — to communicate what elements do without instructions. If a user has to read a manual, the design failed.

## 2. Visual Hierarchy
Everything competes for attention; you decide who wins. Use **size**, **position**, and **color** to rank importance:
- Most important: large, bold, top, colorful
- Least important: small, muted, below
- Images accelerate scanning — use them whenever possible
- Contrast creates hierarchy — no contrast means no hierarchy

## 3. Spacing & Grids
Whitespace is not wasted space — it's breathing room. Grids are guidelines, not laws. What matters:
- Use a **4-point grid** (multiples of 4) for consistency and easy halving
- Group related elements closer together (proximity = relationship)
- Rigid 12-column grids are most useful for responsive/repeating layouts, not custom pages

## 4. Typography
You almost never need more than one font. Pick a clean sans-serif and commit to it. Key rules:
- Max **6 font sizes** for websites; much tighter range for dashboards (rarely above 24px)
- Large display text: tighten letter-spacing (-2 to -3%) and line-height (110–120%) for an instant polish boost
- Font size range should shrink as information density increases

## 5. Color
Start with one brand color, then derive:
- **Lighter** → backgrounds
- **Darker** → text
- Build a **color ramp** from there for chips, states, and charts

Use **semantic color** with purpose:
- Blue = trust, Red = danger/urgency, Yellow = warning, Green = success

Color should carry meaning, not just decoration.

## 6. Dark Mode
Invert your assumptions from light mode:
- No shadows → use **lighter cards on darker backgrounds** to create depth
- Reduce border contrast — light borders are too harsh
- Desaturate bright chips; bump text brightness instead
- Dark mode has more flexibility for deep purples, greens, and reds beyond the typical navy/gray

## 7. Shadows
Shadows communicate depth and elevation — not decoration. Rules:
- Too strong = distraction; reduce opacity, increase blur
- Cards need subtle shadows; popovers/modals need stronger ones
- Inner/outer shadows can create tactile, raised button effects
- If the shadow is the first thing you notice, it's too strong

## 8. Icons & Buttons
- Match icon size to the **line-height of adjacent text** (e.g., 24px text → 24px icons)
- Ghost buttons (no background) work for secondary CTAs; add background for primary
- Button padding guideline: **width ≈ 2× height**

## 9. Feedback & States
Every user action must get a response. Minimum states to design:
- **Buttons**: default, hover, active/pressed, disabled, (loading)
- **Inputs**: default, focused, error, warning
- **Data**: loading spinners, empty states, success messages

If nothing changes when the user acts, the design is broken.

## 10. Micro-interactions
Micro-interactions are feedback with personality. They confirm actions, guide attention, and make interfaces feel alive — from a "copied!" chip sliding up on clipboard copy, to scroll animations, to swipe responses. Range from purely functional to delightfully playful.

## 11. Overlays
Never put text directly on a busy image. Instead:
- Use a **linear gradient** to transition from image to readable background
- Layer a **progressive blur** over the gradient for a modern, polished feel

---

**Core principle:** Design is communication. Every pixel either clarifies or obscures. Default to clarity.
