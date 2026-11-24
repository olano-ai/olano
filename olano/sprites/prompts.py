"""Single source of truth for sprite-generation prompts.

Imported by:
  - olano/runtime/api.py        — production sprite generation
  - scripts/debug_sprite_gen.py    — standalone debugging / iteration script

The dashboard JS mirrors these as `CHARGEN_PAGE1_PROMPT` / `CHARGEN_PAGE2_PROMPT`
constants — keep them in sync if you edit the prompts here.
"""

PAGE1_PROMPT = """\
Use the uploaded reference image as the identity reference for the SAME character across the entire sprite sheet.

STYLE TARGET:
Match the cute, simple, less-detailed top-down RPG pixel sprite style of the previous Page 2 v2 result:
- chibi office-worker proportions
- large head
- compact small body
- cute simplified face
- clean dark outlines
- simple shading
- low-detail clothing
- readable 16-bit RPG overworld sprite
- not realistic
- not highly detailed
- not tall adult proportions
- not isometric
- not painterly

IMPORTANT:
Preserve the recognizable identity and species traits from the reference image while transforming the character into a humanoid office worker suitable for a top-down RPG game.

The character should:
- stand upright on two legs
- have cute chibi human-like body proportions
- wear the SAME casual office-worker clothes in every frame
- maintain the recognizable identity and silhouette from the reference
- keep the same head size, same face, same beard/markings/features, same clothing, same body scale

4-FRAME-WALK-RULES
Frame 1: Contact (Left Leg Forward): The left foot strikes the ground while the right arm moves forward in opposition.
Frame 2: Passing (Middle): The arms and legs align in the center as the weight shifts forward.
Frame 3: Contact (Right Leg Forward): The right foot strikes the ground while the left arm moves forward.
Frame 4: Passing (Middle): The character returns to a centered position, completing the cycle before looping back to Frame 1.

CLOTHING LOCK:
Use the exact same outfit in every frame:
Do not change to shirt, pants, dress or shoes.

SHEET FORMAT:
Exactly 4 rows and 4 columns.
16 equal cells total.
No borders.
No labels.
No text.

ROW LAYOUT:
Row 1 = walking LEFT - CRITICAL:**must follow the 4-FRAME-WALK-RULES**
Row 2 = walking RIGHT - CRITICAL:**must follow the 4-FRAME-WALK-RULES**
Row 3 = walking UP
Row 4 = walking DOWN

ANIMATION RULES:
- smooth simple 4-frame RPG walk cycle - CRITICAL:**must follow the 4-FRAME-WALK-RULES**
- only arms and legs move
- torso remains locked and stable
- head remains stable
- same body height in all 16 cells
- same body width in all 16 cells
- same head size in all 16 cells
- same clothing in all 16 cells
- feet aligned to the same ground line within each row
- character centered in every cell
- full body visible in every frame
- equal padding around sprite
- no limb crossing outside cells
- no scaling changes
- no anatomy drift
- no camera changes
- no perspective changes

CRITICAL:
The output must look like the same exact cute chibi RPG character copied across all frames, with only the walking pose changing.

CRITICAL:**must follow the 4-FRAME-WALK-RULES**

BACKGROUND:
solid flat #FF00FF magenta only.
No shadows.
No gradients.
No props.
No text.
"""

PAGE2_PROMPT = """\
You are given TWO reference images, in this order:
  1. The character PORTRAIT (close-up of face/upper body).
  2. PAGE 1 — the WALK-CYCLE sprite sheet for THIS SAME CHARACTER, generated
     in the previous step. It shows the character in 16 cells (4 directions ×
     4 walk frames) on a magenta background.

═══════════════════════════════════════════════════════
CHARACTER LOCK — match Page 1 EXACTLY
═══════════════════════════════════════════════════════

The character on this Page 2 sheet must be VISUALLY IDENTICAL to the
character on Page 1. This is a hard constraint. Specifically copy from Page 1:
  - exact head size and shape
  - exact facial features (eyes, nose, mouth, eyebrows, beard / markings)
  - exact hair colour, length, and style
  - exact body proportions (head : torso : leg ratio)
  - exact clothing: garment cuts, colours, palette, accessories
  - exact skin tone and outline weight
  - exact pixel-art style and shading

If you would draw the character even slightly differently than Page 1,
DO NOT — copy Page 1's character pixel-for-pixel and only change the pose.

STYLE TARGET (must also match Page 1):
- chibi office-worker proportions
- large head
- compact small body
- cute simplified face
- clean dark outlines
- simple shading
- low-detail clothing
- readable 16-bit RPG overworld sprite
- not realistic
- not highly detailed
- not tall adult proportions
- not isometric
- not painterly
- chibi office-worker proportions
- large head
- compact small body
- cute simplified face
- clean dark outlines
- simple shading
- low-detail clothing
- readable 16-bit RPG overworld sprite
- not realistic
- not highly detailed
- not tall adult proportions
- not isometric
- not painterly

IMPORTANT:
Preserve the recognizable identity and species traits from the reference image while transforming the character into a humanoid office worker suitable for a top-down RPG game.

The character should:
- have cute chibi human-like body proportions
- wear the SAME casual office-worker clothes in every frame
- maintain the recognizable identity and silhouette from the reference
- keep the same head size, same face, same beard/markings/features, same clothing, same body scale

4-FRAME WALK LEFT AND WRITE CYCLE STRUCTURE:
Frame 1: Contact (Left Leg Forward): The left foot strikes the ground while the right arm moves forward in opposition.
Frame 2: Passing (Middle): The arms and legs align in the center as the weight shifts forward.
Frame 3: Contact (Right Leg Forward): The right foot strikes the ground while the left arm moves forward.
Frame 4: Passing (Middle): The character returns to a centered position, completing the cycle before looping back to Frame 1.

CLOTHING LOCK:
Use the exact same outfit in every frame:
Do not change to shirt, pants, dress or shoes.

SHEET FORMAT:
Exactly 4 rows and 4 columns.
16 equal cells total.
No borders.
No labels.
No text.

ROW LAYOUT:
Row 1 = compact sleeping animation
Row 2 = standing drinking coffee animation
Row 3 = standing thinking animation
Row 4 = front-facing seated laptop working animation

FRAME LAYOUT:
Column 1 = start pose
Column 2 = mid pose
Column 3 = strongest pose
Column 4 = return pose

ROW 1 SLEEPING:
The character is sleeping in a compact seated or curled resting pose.
Keep the whole body compact and centered.
No sofa, no bed, no pillow, no blanket.
Subtle breathing only.
Do not add Z letters, text, symbols, bubbles, or icons.

ROW 2 DRINKING COFFEE:
The character remains STANDING UPRIGHT in all 4 frames.
Small coffee cup held close to the chest and mouth.
The cup must stay inside the same body silhouette width.
Do not extend the arm far outward.
Do not make the cup large.
Keep body height and width identical to the standing thinking row.

ROW 3 THINKING:
The character remains STANDING UPRIGHT in all 4 frames.
Subtle thinking gesture only, such as hand near chin.
Do not add thought bubbles, icons, dots, symbols, question marks, or text.
Keep body height and width identical to the coffee row.

ROW 4 LAPTOP WORK:
The character is sitting FRONT-FACING toward the camera on a simple small chair.
The laptop is centered directly in front of the torso, not to the left or right.
The laptop must fit completely inside the same sprite bounding box width as the standing rows.
The laptop must NOT extend outside the character’s normal shoulder-and-arm width.
The laptop must NOT make the sprite wider than the standing coffee or thinking sprites.
The laptop must be small, compact, and close to the body.
Hands stay close together on the keyboard.
Chair is small and mostly hidden behind the character.
Do not use side-facing laptop pose.
Do not use a desk.
Do not make a wide laptop.
Do not let the laptop touch or cross the cell edge.

STRICT WIDTH RULE:
For every cell in Row 4, the total visible width of character + chair + laptop must be equal to or narrower than the standing character width in Rows 2 and 3.
This is mandatory because the sprites will be cropped into equal-width game frames.

ANIMATION RULES:
- smooth simple looping office animations
- subtle movement only
- same body scale in all 16 cells
- same head size in all 16 cells
- same clothing in all 16 cells
- torso remains stable
- head remains stable
- no anatomy drift
- no changing proportions
- no scaling changes
- body centered in every cell
- equal padding around sprite
- no limb, cup, chair, laptop, or prop crossing outside cells
- no text or symbols anywhere

CRITICAL:
The output must look like the same exact cute chibi RPG character copied across all frames, with only the pose changing.
The laptop row must be compact and must not be wider than the standing rows.

4-FRAME WALK LEFT AND WRITE CYCLE STRUCTURE:
Frame 1: Contact (Left Leg Forward): The left foot strikes the ground while the right arm moves forward in opposition.
Frame 2: Passing (Middle): The arms and legs align in the center as the weight shifts forward.
Frame 3: Contact (Right Leg Forward): The right foot strikes the ground while the left arm moves forward.
Frame 4: Passing (Middle): The character returns to a centered position, completing the cycle before looping back to Frame 1.

BACKGROUND:
solid flat #FF00FF magenta only.
No shadows.
No gradients.
No text.
No labels.
No icons.
No speech bubbles.
No thought bubbles.
No Z letters.
"""
# solid flat #FF00FF magenta only.

PORTRAIT_PROMPT_BASE = (
    "Cute chibi character portrait, head and upper torso, centred, square "
    "composition. Large head, compact body, big expressive eyes, soft smile. "
    "Clean dark outlines, simple flat shading, low-detail clothing. "
    "Readable 16-bit RPG art style. Not realistic, not painterly, not isometric, "
    "not tall adult proportions. Same outfit and features as the character sprites. "
    "Solid soft colour background. No text, no watermarks."
)


def build_user_input(description: str = "", emoji: str = "", style: str = "") -> str:
    """Build the user-input suffix that gets appended to each prompt."""
    parts: list[str] = []
    if description:
        parts.append(f"CHARACTER: {description.strip()}")
    if emoji:
        parts.append(f"Character inspired by the emoji: {emoji}")
    if style:
        parts.append(f"Additional style guidance: {style.strip()}")
    return "\n".join(parts)


def with_user_input(base: str, user_input: str) -> str:
    return base + (f"\n\n{user_input}" if user_input else "")
