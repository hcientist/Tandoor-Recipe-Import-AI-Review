# Tandoor Recipe Import AI Review

A backend plugin for [Tandoor Recipes](https://github.com/TandoorRecipes/recipes) 2.6.x. When you import a
recipe with the bookmarklet, your space's default AI provider takes a first pass at the ingredients.
The import review screen then opens on the AI's version instead of the raw parser output.

The first pass:

- re-parses every ingredient line into amount / unit / food / note. For example, "½ to 1 cup heavy cream"
  becomes `0.5 cup heavy cream` with the note "½ to 1 cup; to taste". "2 garlic cloves" becomes `2 cloves garlic`,
  not unit "garlic", food "cloves".
- gives counted items the unit `ea` and moves size words into the note: "1 large pomegranate" becomes
  `1 ea pomegranate` with the note "large", and "3 Persian cucumbers" becomes `3 ea Persian cucumbers`.
- keeps important details out of the note, because Tandoor only shows notes as a tooltip. Container
  sizes go into the unit (`1 15-ounce can chickpeas`), and lines that name two things to buy become two rows
  ("zest and juice of 1 lime", "cheddar and chives, for serving").
- puts each ingredient under the first step that uses it. By default Tandoor puts them all in step one.

Everything else in the import (name, steps, images, keywords, nutrition) is left alone.

## Requirements

- Tandoor 2.6.x (tested on 2.6.15)
- an AI provider set as the space's **Default AI Provider** (Settings → Space → AI), with credits remaining

The plugin reuses Tandoor's own provider settings, LiteLLM and credit accounting. Each request is logged
in the AI log as `IMPORT_FIRST_PASS`. With `claude-haiku-4-5` a typical recipe costs under one credit (1 credit = 1¢).

## Install

Tandoor loads any Django app placed in `/opt/recipes/recipes/plugins/`. With docker compose, clone this repo
next to your compose file and bind-mount it:

```yaml
services:
  web_recipes:
    volumes:
      - ./plugins/ai_first_pass:/opt/recipes/recipes/plugins/ai_first_pass:ro
```

Then recreate the container (`docker compose up -d web_recipes`). The container log should show
`[ai_first_pass] installed on RecipeUrlImportView.post`. The folder name must be `ai_first_pass`.

To uninstall, remove the mount and recreate the container.

## Safety

Tandoor's regular parse is always the fallback:

- If there's no default provider, AI is disabled, or credits are used up, the plugin does nothing.
- On an error, invalid JSON, or a timeout (45 s, to stay under the usual 60 s proxy timeouts), the
  import comes back exactly as Tandoor parsed it.
- If a line is missing from the AI's answer, it keeps its regular parse.
- If the AI's food name uses words that don't appear in the original line, that line also keeps its regular
  parse. This stops the model from swapping ingredients, for example "dark brown sugar" for "light brown sugar".

## Limitations

- Only bookmarklet imports are processed. URL imports and AI imports are untouched.
- Section headers ("For the sauce:") can't be carried through Tandoor's import screen, so those lines are dropped
  and their ingredients are grouped by step instead.
- The plugin wraps `RecipeUrlImportView.post` at startup. A future Tandoor release that changes that view could
  stop it from running, but it would then simply fall back to the regular import.
