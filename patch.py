"""
After a bookmarklet import has been scraped, let the space's default AI provider re-parse the
ingredient lines and assign them to steps, so the import review screen opens on the AI's version.

Any failure (no provider, no credits, timeout, bad JSON) leaves Tandoor's own parse untouched.
"""
import json
import re
import time
import traceback
from fractions import Fraction

import litellm
from django.db.models import Count
from litellm import completion

AI_LOG_FUNCTION = 'IMPORT_FIRST_PASS'
# the import page goes through two nginx hops with 60s read timeouts, and scraping has already taken some of that
AI_TIMEOUT_SECONDS = 45
KNOWN_UNITS_LIMIT = 120

RULES = """You clean up ingredient lists for the Tandoor recipe manager.
Each ingredient row has amount (number), unit, food and note. In Tandoor, amount, unit and food are shown;
the note is hidden behind a small tooltip, so nothing important may live only in the note.

Rules:
1. amount is one number (0.5 for 1/2, 1.5 for 1 1/2, 0.333 for 1/3). Use 0 when there is no amount ("salt, to taste").
2. Ranges ("1/2 to 1 cup", "2-3 cloves", "(10- to 12-ounce)"): amount is the LOWER bound; start the note with the full range, e.g. "1/2 to 1 cup".
3. "X plus Y" of the same food used at once ("1/4 cup plus 2 tablespoons vinegar"): one row, total amount in the smaller unit ("6 tablespoons"), original wording in the note.
4. unit is a measuring unit or container: cup(s), tablespoon(s), teaspoon(s), ounce(s), pound(s), g, ml, clove(s), stalk(s), sprig(s), pinch, dash, handful, bunch, head, slices, ears, can, package...
   - Match the amount: "1 cup" but "2 cups". Prefer a name from KNOWN UNITS when one fits.
   - Size words (small, medium, large, whole) may be the unit when there is no other unit ("2 large eggs").
   - Never use a food, colour, variety or preparation word as the unit ("2 garlic cloves" is unit "cloves", food "garlic"; "3 celery stalks" is unit "stalks", food "celery"; "8 boneless skinless chicken thighs" has no unit).
   - A container size belongs in the unit: "1 (15-ounce) can chickpeas" is unit "15-ounce can", food "chickpeas"; "1 (2-inch) piece ginger" is unit "2-inch piece".
   - "Pinch of salt" is amount 1, unit "pinch", food "salt".
   - When amount is 0, leave the unit empty.
5. food is what you would buy: short, no amounts, no metric equivalents, no preparation, no "for serving".
   - Use the recipe's own words for the food. Never swap in a similar ingredient (dark brown sugar stays dark brown sugar).
   - Keep words that change what you buy: "unsalted butter", "smoked paprika", "extra-virgin olive oil", "low-sodium soy sauce", "smooth peanut butter".
   - Alternatives ("peanut or vegetable oil", "honey or maple syrup"): food is the first option, the note gets "or vegetable oil".
6. note holds everything else, parts separated by "; ": preparation (chopped, softened, at room temperature), metric or volume equivalents ("225g", "about 2 cups"), alternatives, "optional", "divided", "for serving", brand suggestions, "see note".
   Keep the source wording, keep it short, never invent information.
7. Never hide a second ingredient in a note. When one line names several things to buy, output one row per thing, all with the same "line":
   "zest and juice of 1 lime" is lime zest + lime juice; "1 large egg plus 1 egg yolk" is 2 rows;
   "avocado, cilantro and lime wedges, for serving" is 3 rows. A second quantity of the same food used differently
   ("1/2 cup cold butter, cubed, plus 6 tablespoons melted butter") is also its own row.
8. Section header lines ("For the sauce:", "To serve:") are not ingredients: put their line numbers in "skipped".
9. step is the 0-based index of the first instruction step that uses the ingredient. Use 0 when unsure.
10. Keep rows in the original order. Every line number must appear in at least one row or in "skipped".

Examples (line text -> rows):
"1/2 to 1 cup heavy cream, to taste" -> {"amount":0.5,"unit":"cup","food":"heavy cream","note":"1/2 to 1 cup; to taste"}
"2 teaspoons (10ml) fresh juice from 1 lime" -> {"amount":2,"unit":"teaspoons","food":"lime juice","note":"10ml; fresh, from 1 lime"}
"1/2 cup/115 grams cold unsalted butter (1 stick), cubed" -> {"amount":0.5,"unit":"cup","food":"unsalted butter","note":"115 grams (1 stick); cold, cubed"}
"2 (14-ounce) packages extra-firm tofu, drained" -> {"amount":2,"unit":"14-ounce package","food":"extra-firm tofu","note":"drained"}
"1 cup smooth, natural peanut butter" -> {"amount":1,"unit":"cup","food":"smooth natural peanut butter","note":""}
"2 tablespoons apple cider, rice wine or white wine vinegar" -> {"amount":2,"unit":"tablespoons","food":"apple cider vinegar","note":"or rice wine or white wine vinegar"}

Return only JSON: {"rows":[{"line":0,"step":0,"amount":0.5,"unit":"cup","food":"heavy cream","note":"..."}],"skipped":[]}"""


def install():
    from cookbook.views import api as api_views

    view = api_views.RecipeUrlImportView
    if getattr(view.post, '_ai_first_pass', False):
        return
    original_post = view.post

    def post(self, request, *args, **kwargs):
        response = original_post(self, request, *args, **kwargs)
        try:
            if request.data.get('bookmarklet') and response.status_code == 200 and response.data.get('recipe'):
                first_pass(request, response.data['recipe'])
        except Exception:
            print('[ai_first_pass] failed, keeping the regular parse')
            traceback.print_exc()
        return response

    post._ai_first_pass = True
    view.post = post
    print('[ai_first_pass] installed on RecipeUrlImportView.post')


def first_pass(request, recipe):
    from cookbook.helper.ai_helper import AiCallbackHandler, can_perform_ai_request
    from cookbook.models import Unit
    from recipes.settings import AI_ALLOWED_URLS

    space = request.space
    provider = space.ai_default_provider
    if provider is None or not space.ai_enabled or not can_perform_ai_request(space):
        print('[ai_first_pass] skipped: no default AI provider, AI disabled, or no credits left')
        return

    steps = recipe.get('steps') or []
    sources = [i for s in steps for i in (s.get('ingredients') or [])]
    if not sources:
        return
    lines = [(i.get('original_text') or _describe(i)).strip() for i in sources]

    known_units = list(Unit.objects.filter(space=space).annotate(n=Count('ingredient')).filter(n__gt=0)
                       .order_by('-n').values_list('name', flat=True)[:KNOWN_UNITS_LIMIT])

    task = {
        'recipe': recipe.get('name', ''),
        'steps': [f'{n}: {(s.get("instruction") or "")[:1500]}' for n, s in enumerate(steps)],
        'lines': [f'{n}: {text}' for n, text in enumerate(lines)],
    }
    messages = [
        {'role': 'system', 'content': RULES},
        {'role': 'user', 'content': 'KNOWN UNITS: ' + ' | '.join(known_units) + '\n\n' + json.dumps(task, ensure_ascii=False)},
    ]
    ai_request = {
        'api_key': provider.api_key,
        'model': provider.model_name,
        'response_format': {'type': 'json_object'},
        'messages': messages,
        'timeout': AI_TIMEOUT_SECONDS,
        'max_tokens': 8000,
    }
    if provider.url:
        if provider.url not in AI_ALLOWED_URLS:
            print(f'[ai_first_pass] skipped: provider URL not in AI_ALLOWED_URLS')
            return
        ai_request['api_base'] = provider.url

    litellm.callbacks = [AiCallbackHandler(space, request.user, provider, AI_LOG_FUNCTION)]
    started = time.monotonic()
    ai_response = completion(**ai_request)
    result = _load_json(ai_response.choices[0].message.content)

    new_steps = [[] for _ in steps]
    covered = set(int(x) for x in result.get('skipped', []) if _is_int(x))
    rejected = set()
    rows = result.get('rows', [])
    for row in rows:
        line, food = row.get('line'), (row.get('food') or '').strip()
        if _is_int(line) and 0 <= int(line) < len(lines) and food and not _words_from(food, lines[int(line)]):
            rejected.add(int(line))
    for row in rows:
        line = row.get('line')
        food = (row.get('food') or '').strip()
        if not _is_int(line) or not 0 <= int(line) < len(lines) or not food:
            continue
        line = int(line)
        if line in rejected:
            continue
        step = int(row['step']) if _is_int(row.get('step')) and 0 <= int(row['step']) < len(steps) else 0
        unit = (row.get('unit') or '').strip()
        new_steps[step].append({
            'amount': _amount(row.get('amount')),
            'food': {'name': food[:128]},
            'unit': {'name': unit[:128]} if unit else None,
            'note': (row.get('note') or '').strip()[:256],
            'order': None,
            'original_text': lines[line],
        })
        covered.add(line)

    # never lose a line the model forgot: keep Tandoor's own parse for it in the first step
    missing = [n for n in range(len(lines)) if n not in covered or n in rejected]
    for n in missing:
        new_steps[0].append(sources[n])

    for s, ingredients in zip(steps, new_steps):
        s['ingredients'] = ingredients
    print(f'[ai_first_pass] {recipe.get("name", "")!r}: {len(lines)} lines -> '
          f'{sum(len(x) for x in new_steps)} rows over {sum(1 for x in new_steps if x)} steps, '
          f'{len(missing)} kept from regular parse ({len(rejected)} rejected for words not in the line), '
          f'{time.monotonic() - started:.1f}s')


STOPWORDS = {'and', 'or', 'of', 'the', 'a', 'an', 'with', 'for', 'in'}


def _stem(word):
    for suffix in ('es', 's'):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[:-len(suffix)]
    return word


def _words_from(food, line):
    """True when every word of the food appears in the source line, so the model cannot substitute an ingredient."""
    line_words = re.findall(r'\w+', line.lower())
    line_stems = {_stem(w) for w in line_words}
    for word in re.findall(r'\w+', food.lower()):
        if word in STOPWORDS or word.isdigit():
            continue
        if _stem(word) in line_stems:
            continue
        if len(word) >= 4 and any(w.startswith(word[:-1]) for w in line_words):
            continue
        return False
    return True


def _describe(ingredient):
    unit = (ingredient.get('unit') or {}).get('name', '')
    food = (ingredient.get('food') or {}).get('name', '')
    return ' '.join(str(x) for x in (ingredient.get('amount') or '', unit, food, ingredient.get('note') or '') if x)


def _load_json(text):
    text = text.strip()
    fenced = re.match(r'^```(?:json)?\s*(.*?)\s*```$', text, re.S)
    return json.loads(fenced.group(1) if fenced else text)


def _is_int(value):
    try:
        int(value)
        return not isinstance(value, bool)
    except (TypeError, ValueError):
        return False


def _amount(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(float(value), 0.0)
    try:
        return max(float(sum(Fraction(p) for p in str(value).split())), 0.0)
    except (ValueError, ZeroDivisionError):
        return 0.0
