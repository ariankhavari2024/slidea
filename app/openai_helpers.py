# app/openai_helpers.py
import os
import json
import re
import requests
import uuid
import logging
import base64
import random
from datetime import datetime

try:
    from openai import OpenAI, OpenAIError, APIConnectionError, RateLimitError, APIStatusError
except ImportError:
    raise ImportError("OpenAI library not found. Please install it using: pip install openai>=1.0.0")
from flask import current_app, url_for

# --- get_openai_client (No changes needed) ---
def get_openai_client():
    """Initializes and returns the OpenAI client."""
    api_key = current_app.config.get('OPENAI_API_KEY')
    if not api_key:
        current_app.logger.error("OpenAI API key not configured.")
        raise ValueError("OpenAI API key not configured.")
    try:
        client = OpenAI(api_key=api_key, timeout=120.0, max_retries=1)
        return client
    except Exception as e:
        current_app.logger.error(f"Failed to initialize OpenAI client: {e}", exc_info=True)
        raise ValueError(f"Failed to initialize OpenAI client: {e}")

# --- log_prompt_to_file (No changes needed) ---
def log_prompt_to_file(log_type, prompt_data):
    """Appends prompt data to the configured log file."""
    log_file_path = current_app.config.get('PROMPT_LOG_FILE')
    if not log_file_path: return
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f"--- {log_type} Log Entry [{timestamp}] ---\n"
        if isinstance(prompt_data, dict):
            for key, value in prompt_data.items(): log_entry += f"{key}:\n{value}\n\n"
        else: log_entry += f"{prompt_data}\n\n"
        log_entry += "---\n\n"
        log_dir = os.path.dirname(log_file_path)
        if log_dir and not os.path.exists(log_dir): os.makedirs(log_dir)
        with open(log_file_path, 'a', encoding='utf-8') as f: f.write(log_entry)
    except Exception as e:
        current_app.logger.error(f"Error during prompt logging: {e}", exc_info=True)


# --- generate_text_content (UPDATED with stricter slide count instructions) ---
def generate_text_content(topic: str, text_style: str = 'bullet', desired_slide_count: int = 10, presenter_name: str | None = None) -> list[dict]:
    """
    Generates structured slide text content using OpenAI API.
    The first slide will contain only the title and presenter name (if provided).
    Subsequent slides will contain generated content based on the text_style.
    *** NOW WITH STRICTER SLIDE COUNT ADHERENCE ***
    """
    client = get_openai_client()
    response_content = "[No response content obtained]"
    json_string = "[JSON not extracted]"

    # Determine instructions for slides *after* the first one
    if text_style == 'bullet':
        style_instruction_subsequent = "a JSON list of short, informative bullet points (5-6 concise bullets, max 15-20 words each)"
        content_format_example_subsequent = '"slide_content": ["Co-founded Apple with Steve Wozniak in 1976", "Revolutionized personal computing with Macintosh", "Led Pixar to animation success", "Introduced iPod, iPhone, iPad changing industries", "Known for design focus and reality distortion field"]'
        first_slide_content_value = [] # Empty list for bullet style
    elif text_style == 'paragraph':
        style_instruction_subsequent = "a single, coherent string paragraph (one clear paragraph, approx. 60-90 words)"
        content_format_example_subsequent = '"slide_content": "Padel, a dynamic blend of tennis and squash, is rapidly gaining global popularity. Played on an enclosed court with walls integral to gameplay, it emphasizes social interaction and strategy. Typically played in doubles with underhand serves, it\'s accessible yet offers depth. Its ease of learning, social nature, and fitness benefits contribute to its worldwide explosion."'
        first_slide_content_value = f"By: {presenter_name.strip()}" if presenter_name and presenter_name.strip() else "" # Presenter name or empty string
    else:
        current_app.logger.warning(f"Unrecognized text_style '{text_style}', defaulting to bullet points.")
        text_style = 'bullet'
        style_instruction_subsequent = "a JSON list of short, informative bullet points (5-6 concise bullets, max 15-20 words each)"
        content_format_example_subsequent = '"slide_content": ["Co-founded Apple with Steve Wozniak in 1976", "Revolutionized personal computing with Macintosh", "Led Pixar to animation success", "Introduced iPod, iPhone, iPad changing industries", "Known for design focus and reality distortion field"]'
        first_slide_content_value = []

    # *** UPDATED System Prompt (Stricter Slide Count) ***
    system_prompt = f"""
You are an expert presentation creator AI, tasked with generating accurate, engaging, and well-structured slide content for a presentation about "{topic}".
Generate content for **EXACTLY** {desired_slide_count} slide(s) in total. This is a strict requirement.
Return the content in this **exact JSON format**: a single JSON list where each object represents a slide.

**Slide Structure Rules:**
1.  **First Slide (Slide 1):**
    * MUST have "slide_number": 1.
    * MUST have a "slide_title" (e.g., the presentation topic).
    * MUST have "slide_content": {json.dumps(first_slide_content_value)} (This specific value, representing presenter name or empty).
2.  **Subsequent Slides (Slide 2 onwards, ONLY IF desired_slide_count > 1):**
    * MUST have "slide_number" (incrementing).
    * MUST have a relevant "slide_title".
    * MUST have "slide_content" formatted as: {style_instruction_subsequent}.

**General Rules:**
* **Slide Count Adherence:** **CRITICAL:** Generate **EXACTLY** {desired_slide_count} slide object(s) in the final JSON list. Do NOT add extra slides like introductions or conclusions unless the total count allows for it within the {desired_slide_count} limit. If {desired_slide_count} is 1, ONLY generate the first slide according to Rule 1.
* **Content Quality & Flow:** For slides 2+, generate informative and coherent content that logically progresses. Avoid random facts. Ensure content directly supports the title.
* **Factual Accuracy:** Prioritize accuracy for slides 2+. If unsure, state briefly it's speculative or omit.
* **Logical Flow (if > 1 slide):** If generating multiple slides, create a logical flow (e.g., Introduction -> Key Points -> Conclusion).
* **Strict JSON Format:** Output ONLY the JSON list. Start with '[' end with ']'. Escape strings properly. No introductory text, explanations, or ```json markers.

**Example Output Format (for '{text_style}' style, assuming desired_slide_count > 1):**
[
  {{
    "slide_number": 1,
    "slide_title": "Presentation Topic: {topic}",
    "slide_content": {json.dumps(first_slide_content_value)}
  }},
  {{
    "slide_number": 2,
    "slide_title": "Example Subsequent Title 1",
    "slide_content": {content_format_example_subsequent}
  }}
  // ... potentially more slides up to EXACTLY {desired_slide_count}
]

**Example Output Format (if desired_slide_count is 1):**
[
  {{
    "slide_number": 1,
    "slide_title": "Presentation Topic: {topic}",
    "slide_content": {json.dumps(first_slide_content_value)}
  }}
]
"""

    user_prompt = f"Generate the JSON slide structure for the presentation on '{topic}' following all rules, ensuring EXACTLY {desired_slide_count} slide(s) are generated."

    prompt_log_data = {
        "Type": "Text Generation V6 (Strict Count)",
        "Topic": topic,
        "Style": text_style,
        "Desired Count": desired_slide_count,
        "Presenter Name": presenter_name,
        "System Prompt (Excerpt)": system_prompt[:500] + "...",
        "User Prompt": user_prompt
    }
    log_prompt_to_file("Text Generation Request", prompt_log_data)

    try:
        current_app.logger.info(f"Requesting text content V6 for topic: '{topic}' (Style: {text_style}, EXACT Count: {desired_slide_count})")
        completion = client.chat.completions.create(
            model="gpt-4.1-nano", # Or your preferred model
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.6, # Slightly lower temperature might help adherence
        )
        response_content = completion.choices[0].message.content
        current_app.logger.debug(f"Raw OpenAI text response received.")

        if not response_content:
            raise ValueError("AI returned an empty text response.")

        # Attempt to extract JSON robustly
        json_string = response_content
        if json_string.startswith("```json"): json_string = json_string[7:]
        if json_string.startswith("```"): json_string = json_string[3:]
        if json_string.endswith("```"): json_string = json_string[:-3]
        json_string = json_string.strip()

        json_start = json_string.find('[')
        json_end = json_string.rfind(']')

        if json_start == -1 or json_end == -1 or json_end < json_start:
            current_app.logger.error(f"Failed to find valid JSON list structure in response: {json_string[:500]}...")
            raise ValueError("AI response did not contain a valid JSON list structure ('[...]').")

        json_string = json_string[json_start : json_end + 1]
        current_app.logger.debug(f"Attempting to parse extracted JSON string.")
        slides_data = json.loads(json_string)

        if not isinstance(slides_data, list) or not slides_data:
            raise ValueError("Parsed data is not a non-empty JSON list.")

        actual_content_count = len(slides_data)
        log_prompt_to_file("Text Generation Response Info", f"Topic: {topic}\nDesired Count: {desired_slide_count}\nActual Count Received: {actual_content_count}")

        # *** ADDED: Check if the count matches exactly ***
        if actual_content_count != desired_slide_count:
            current_app.logger.warning(f"AI slide count MISMATCH! Requested: {desired_slide_count}, Got: {actual_content_count}. Will truncate or pad if possible, but this indicates a prompt adherence issue.")
            # Optionally, you could truncate the list if it's too long:
            # slides_data = slides_data[:desired_slide_count]
            # Or raise an error if exact count is critical:
            # raise ValueError(f"AI did not generate the exact number of slides requested ({desired_slide_count}). Got {actual_content_count}.")
            # For now, we'll proceed with validation but log the warning.

        # VALIDATE AND NORMALIZE
        validated_slides = []
        for i, slide in enumerate(slides_data):
             # Stop processing if we already have the desired number (handles truncation case)
            if len(validated_slides) >= desired_slide_count:
                break

            if not isinstance(slide, dict):
                current_app.logger.warning(f"Slide data at index {i} is not a dict. Skipping.")
                continue

            keys_lower = {k.lower(): v for k, v in slide.items()}
            if 'slide_title' not in keys_lower:
                current_app.logger.warning(f"Slide object at index {i} missing 'slide_title'. Skipping.")
                continue

            if 'slide_title' not in slide: slide['slide_title'] = keys_lower['slide_title']
            if 'slide_content' not in keys_lower:
                 current_app.logger.warning(f"Slide object at index {i} missing 'slide_content'. Setting to default.")
                 slide['slide_content'] = "" if text_style == 'paragraph' else []
            elif 'slide_content' not in slide:
                 slide['slide_content'] = keys_lower['slide_content']

            # Assign slide number based on its position in the *validated* list
            slide['slide_number'] = len(validated_slides) + 1

            # Enforce First Slide Content
            if slide['slide_number'] == 1:
                slide['slide_content'] = first_slide_content_value
                if not slide['slide_title'] or slide['slide_title'] == "Example Title 1":
                    slide['slide_title'] = f"Presentation on: {topic}"
            # Normalize content for subsequent slides
            elif slide['slide_number'] > 1:
                content = slide['slide_content']
                if text_style == 'bullet':
                    if not isinstance(content, list):
                        if isinstance(content, str):
                            bullets = [line.strip() for line in content.split('\n') if line.strip()]
                            slide['slide_content'] = bullets if bullets else [" "]
                        else: slide['slide_content'] = ["[Content format error]"]
                elif text_style == 'paragraph':
                    if not isinstance(content, str):
                        if isinstance(content, list): slide['slide_content'] = " ".join(content)
                        else: slide['slide_content'] = "[Content format error]"

            validated_slides.append(slide)

        if not validated_slides:
             raise ValueError("No valid slide data could be extracted from the AI response.")

        # Final check if the validated count matches desired (especially if truncation happened)
        if len(validated_slides) != desired_slide_count:
             current_app.logger.warning(f"Final validated slide count ({len(validated_slides)}) still differs from desired ({desired_slide_count}).")


        current_app.logger.info(f"Successfully parsed and validated {len(validated_slides)} slides from AI text response.")
        return validated_slides # Return the validated (potentially truncated) list

    except json.JSONDecodeError as e:
        current_app.logger.error(f"JSON Decode Error: {e}. Response content: {json_string[:500]}...")
        raise ValueError("AI response was not valid JSON.") from e
    except RateLimitError as e:
        current_app.logger.error(f"OpenAI Rate Limit Error: {e}")
        raise ConnectionError("OpenAI API rate limit exceeded.") from e
    except APIConnectionError as e:
        current_app.logger.error(f"OpenAI Connection Error: {e}")
        raise ConnectionError("Failed to connect to OpenAI API.") from e
    except APIStatusError as e:
        current_app.logger.error(f"OpenAI API Status Error: Status={e.status_code}, Response={e.response}")
        raise ConnectionError(f"OpenAI API returned an error (Status {e.status_code}).") from e
    except OpenAIError as e:
        current_app.logger.error(f"OpenAI API Error: {e}")
        raise ConnectionError("An error occurred communicating with OpenAI API.") from e
    except ValueError as e:
        current_app.logger.error(f"Data validation error: {e}")
        raise
    except Exception as e:
        current_app.logger.error(f"Unexpected error in generate_text_content: {e}", exc_info=True)
        raise


# --- generate_missing_slide_content (No changes needed) ---
def generate_missing_slide_content(slide_title: str, text_style: str = 'bullet', presentation_topic: str | None = None) -> str | list:
    """
    Generates content for a single slide when manually entered content is blank.
    Now includes presentation_topic for context.
    """
    client = get_openai_client()
    current_app.logger.info(f"Generating missing content for slide title: '{slide_title}' (Style: {text_style}, Topic: '{presentation_topic}')")

    if text_style == 'bullet':
        style_instruction = "a JSON list of short, informative bullet points (5-6 concise bullets, max 15-20 words each) relevant to the slide title"
        content_format_example = '["Point 1 about the title", "Point 2", "Point 3", "Point 4", "Point 5"]'
        expected_type = list
    else: # paragraph
        style_instruction = "a single, coherent string paragraph (one clear paragraph, approx. 60-90 words) expanding on the slide title"
        content_format_example = '"A concise paragraph explaining the key idea of the slide title..."'
        expected_type = str

    system_prompt = f"""
You are an AI assistant helping to fill in missing presentation content.
The overall presentation topic is: "{presentation_topic or 'Not specified'}".
The user provided ONLY the slide title for the current slide: "{slide_title}".
Generate ONLY the slide content based on this title AND the overall presentation topic, formatted as {style_instruction}.
Output ONLY the content itself (either the JSON list for bullets or the string paragraph). Do not include the slide title or slide number. No explanations.
Example Format ({text_style}): {content_format_example}
"""
    user_prompt = f"Generate the slide content for the title: '{slide_title}' within the context of the presentation topic: '{presentation_topic or 'Not specified'}'."

    log_prompt_to_file("Missing Content Generation Request", {"Title": slide_title, "Style": text_style, "Topic": presentation_topic})

    try:
        completion = client.chat.completions.create(
            model="gpt-4.1-nano", # Or a faster/cheaper model if suitable
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
        )
        response_content = completion.choices[0].message.content.strip()

        if not response_content:
            raise ValueError("AI returned empty content for missing slide.")

        if text_style == 'bullet':
            try:
                if response_content.startswith("```json"): response_content = response_content[7:]
                if response_content.startswith("```"): response_content = response_content[3:]
                if response_content.endswith("```"): response_content = response_content[:-3]
                response_content = response_content.strip()
                if not response_content.startswith('[') or not response_content.endswith(']'):
                    current_app.logger.warning(f"AI response for bullets wasn't a JSON list for title '{slide_title}'. Splitting by newline. Response: {response_content[:100]}...")
                    parsed_content = [line.strip() for line in response_content.split('\n') if line.strip()] or [" "]
                else:
                    parsed_content = json.loads(response_content)

                if not isinstance(parsed_content, list):
                    current_app.logger.warning(f"Parsed content for bullets is not a list for title '{slide_title}'. Using fallback.")
                    return ["[AI content generation error]"]
                return parsed_content if parsed_content else [" "]

            except json.JSONDecodeError:
                current_app.logger.warning(f"Failed to parse bullet points as JSON for title '{slide_title}'. Splitting by newline. Response: {response_content[:100]}...")
                return [line.strip() for line in response_content.split('\n') if line.strip()] or [" "]
        else: # Paragraph style
            if not isinstance(response_content, str):
                 current_app.logger.warning(f"AI response for paragraph was not a string for title '{slide_title}'. Type: {type(response_content)}")
                 return "[AI content generation error]"
            return response_content

    except Exception as e:
        current_app.logger.error(f"Error generating missing content for title '{slide_title}': {e}", exc_info=True)
        return "[AI content generation error]" if text_style == 'paragraph' else ["[AI content generation error]"]


# --- parse_manual_content (No changes) ---
def parse_manual_content(script_text: str) -> list[dict]:
    """Parses user-provided script text into slides based on 'Title:' or 'Slide X:' markers."""
    slides_data = []
    lines = script_text.strip().split('\n')
    current_title = "Untitled Slide 1"
    current_content_lines = []
    found_structure = False
    slide_counter = 1
    title_pattern = re.compile(r"^\s*(?:slide\s*\d+\s*[:.)-]?|title\s*[:.)-]?)\s*(.*)", re.IGNORECASE)
    for line in lines:
        match = title_pattern.match(line)
        if match:
            found_structure = True
            if current_content_lines:
                processed_content = [l.rstrip() for l in current_content_lines]
                slides_data.append({"slide_title": current_title, "slide_content": processed_content})
            potential_title = match.group(1).strip(); current_title = potential_title if potential_title else f"Untitled Slide {slide_counter}"
            current_content_lines = []; slide_counter += 1
        elif line.strip() or current_content_lines: current_content_lines.append(line)
    if current_content_lines or (found_structure and not slides_data):
        processed_content = [l.rstrip() for l in current_content_lines]
        slides_data.append({"slide_title": current_title, "slide_content": processed_content})
    if not found_structure and script_text.strip():
        current_app.logger.info("No 'Title:' or 'Slide X:' structure detected in manual input. Treating entire input as one slide or splitting by double newline.")
        slides_data = []
        slide_blocks = [block for block in script_text.strip().split('\n\n') if block.strip()]
        if len(slide_blocks) <= 1:
             block_lines = script_text.strip().split('\n')
             title = block_lines[0].strip() if block_lines else "Manual Input Slide 1"
             content_lines = [l.rstrip() for l in block_lines[1:]]
             slides_data.append({"slide_title": title, "slide_content": content_lines})
        else:
            for i, block in enumerate(slide_blocks):
                block_lines = block.split('\n'); title = block_lines[0].strip() if block_lines else f"Manual Input Slide {i+1}"
                content_lines = [l.rstrip() for l in block_lines[1:]]
                slides_data.append({"slide_title": title, "slide_content": content_lines})
    current_app.logger.info(f"Parsed {len(slides_data)} slides from manual input (Structure Found: {found_structure}). Content stored as list of strings.")
    return slides_data


# --- VISUAL GENERATION HELPERS ---

# --- get_style_description (No changes needed for custom logic) ---
def get_style_description(style_key_or_prompt: str) -> str:
    """Returns a detailed description for predefined styles, or the prompt itself if custom."""
    style_map = {
        'keynote_modern': "Clean, modern Apple Keynote aesthetic. Ample white space, elegant sans-serif font (like SF Pro or Helvetica Neue), subtle gradients or solid muted backgrounds, high-quality relevant visuals (photos or icons), focus on clarity and hierarchy. Minimalist but polished.",
        'abstract_gradient': "Vibrant, abstract gradient background (e.g., purple-pink-orange, blue-green). Energetic feel, possibly with subtle geometric shapes or overlays. Modern sans-serif font (e.g., Montserrat, Poppins). Focus on color and dynamism.",
        'minimalist_sketch': "Clean, minimalist design using hand-drawn sketch-style illustrations or icons. Lots of white space. Simple, readable sans-serif font (e.g., Quicksand, Nunito). Limited color palette, often monochrome with one accent color.",
        'cyberpunk_glow': "Futuristic cyberpunk aesthetic. Dark background (deep blues, purples, blacks). Neon glowing elements, grids, digital glitches, holographic effects. Tech-inspired sans-serif font (e.g., Orbitron, Teko). Vibrant neon accent colors (pinks, cyans, greens).",
        'corporate_charts': "Professional corporate style. Clean layout, structured design with potential for simple charts/graphs (bar charts, line graphs) if relevant to content. Use of blues, grays, whites. Clear sans-serif font like Lato or Open Sans. Focus on data visualization and professionalism.",
        'ghibli_inspired': "Warm, whimsical Studio Ghibli-inspired anime aesthetic. Hand-painted watercolor backgrounds, soft lighting, nature motifs (plants, clouds, sky). Gentle, rounded font. Pastel color palette (soft blues, greens, pinks, creams). Evokes nostalgia and wonder.",
        'pencil_paper': "Hand-drawn pencil sketch style on textured paper background. Monochrome or limited color palette (e.g., graphite grey, sepia tones). Illustrations should look sketched. IMPORTANT: Text elements (title, body) should appear as if written in pencil or simple handwriting font.",
        'claymorphism_3d': "Soft, rounded 3D claymorphism style. Elements appear like smooth clay or plasticine objects with soft shadows and inner/outer extrusion effects. Pastel or muted color palette. Playful, tactile feel. Use a friendly, rounded sans-serif font.",
    }
    # If the input key is in the map, return the description.
    # Otherwise, assume the input *is* the custom prompt and return it directly.
    return style_map.get(style_key_or_prompt, style_key_or_prompt)


# --- build_image_prompt (No changes needed for custom style logic) ---
def build_image_prompt(slide_title: str,
                       slide_content: str | list | None,
                       style_description: str, # This will contain the custom prompt if style_choice was 'custom'
                       text_style: str,
                       slide_number: int,
                       total_slides: int,
                       creativity_score: int = 5,
                       presentation_topic: str | None = None,
                       font_choice: str = 'Inter',
                       presenter_name: str | None = None
                       ) -> str:
    """
    Builds image prompt instructing AI to generate visual *with* text for a 3:2 aspect ratio slide.
    Handles first slide differently (only title + presenter name).
    Correctly uses the provided `style_description` whether it's predefined or custom.
    """
    # (Logic remains the same as previous version - openai_helpers_v4)
    is_first_slide = (slide_number == 1)
    is_last_slide = (slide_number == total_slides)
    title_lower = slide_title.lower() if slide_title else ""
    is_likely_closing = any(keyword in title_lower for keyword in ["thank you", "q&a", "conclusion", "summary", "next steps", "final thoughts"])
    slide_type = "Content";
    if is_first_slide: slide_type = "Title"
    elif is_last_slide and is_likely_closing: slide_type = "Closing"

    body_content_text = ""; content_type_description = ""; visual_content_hint = ""
    presenter_name_clean = presenter_name.strip() if presenter_name else None
    has_body_content = False

    if slide_type == "Title":
        content_type_description = "Title Slide"
        visual_content_hint = presentation_topic or slide_title or "main theme"
        body_content_text = f"By: {presenter_name_clean}" if presenter_name_clean else ""
        if body_content_text: has_body_content = True
    elif slide_type == "Closing":
        content_type_description = "Closing Content"
        visual_content_hint = "simple, clean, abstract graphic or background"
        if isinstance(slide_content, list) and slide_content:
            body_content_text = "\n".join([f"- {item}" for item in slide_content])
            has_body_content = True
        elif isinstance(slide_content, str) and slide_content.strip():
            body_content_text = slide_content
            has_body_content = True
    else: # Content slide (Slide 2+)
        visual_content_hint = f"core concept of '{slide_title}'"
        if isinstance(slide_content, list) and slide_content:
            content_type_description = "bullet points"; body_content_text = "\n".join([f"- {item}" for item in slide_content])
            has_body_content = True
            if slide_content: visual_content_hint += f" - {slide_content[0]}"
        elif isinstance(slide_content, str) and slide_content.strip():
            content_type_description = "paragraph"; body_content_text = slide_content; has_body_content = True; visual_content_hint += f" - {slide_content[:80]}..."
        else:
             content_type_description = f"content area (layout suitable for {text_style})"
             body_content_text = ""
             has_body_content = False
             visual_content_hint = f"visual representing '{slide_title}' (no body text provided)"

    layout_description = ""
    text_area_description = "a clearly defined area with high contrast against its background (e.g., a solid panel, shape, or clean zone of the visual)"

    if slide_type == "Title":
        title_placement = "Prominently Top or Center"; name_placement = "Subtly below title, smaller font"; visual_placement = "Main focus or background, visually appealing"
        layout_description = f"Place visual '{visual_placement}', title at '{title_placement}'."
        if presenter_name_clean: layout_description += f" Place presenter name '{name_placement}'."
    elif slide_type == "Closing":
        title_placement = "Top or Center"; text_placement = "Center or Bottom"; visual_placement = "Subtle background or abstract element, minimalist"
        layout_description = f"Place visual '{visual_placement}', title at '{title_placement}', body text (if any) in '{text_placement}' within {text_area_description}."
    else: # Content Slides (Slide 2+)
        layouts_low = [ f"Standard: Visual Left 60-70%, title Top-Right, body text Right 30-40% within {text_area_description}.", f"Standard Reversed: Visual Right 60-70%, title Top-Left, body text Left 30-40% within {text_area_description}.", ]
        layouts_medium = layouts_low + [ f"Top Visual: Visual as Background or Top 60-70%, title Top, body text Bottom 30-40% within {text_area_description}.", f"Centered Text: Visual as Background, title Top-Center, body text Centered within {text_area_description}.", f"Split Vertical: Visual fills top half, text fills bottom half within {text_area_description}.", ]
        layouts_high = layouts_medium + [ f"Dynamic Integrated: Arrange visual, title, and body text creatively (e.g., text integrated near relevant visual parts, overlapping clean areas). Ensure balance, hierarchy, place text within {text_area_description}.", f"Creative Split Screen: Visual on one side (vertical or horizontal split), text artfully arranged on the other within {text_area_description}. Avoid simple 50/50.", f"Full Background Visual: Compelling full-bleed background image, title/text strategically placed in areas of lower visual complexity within {text_area_description}. Use overlays if needed for contrast.", f"Minimalist Focus: Strong central visual, text placed minimally but impactfully (e.g., corner, edge) within {text_area_description}.", f"Asymmetric Balance: Visual dominates one area (e.g., top-left), text balances in another (e.g., bottom-right) within {text_area_description}.", ]
        if 1 <= creativity_score <= 3: layout_description = random.choice(layouts_low)
        elif 4 <= creativity_score <= 7: layout_description = random.choice(layouts_medium)
        elif 8 <= creativity_score <= 10: layout_description = random.choice(layouts_high)
        if slide_number > 2: layout_description += " Try a different composition than the previous slide."
        if not has_body_content:
            layout_description = layout_description.replace("body text", "title")
            layout_description = layout_description.replace(f"within {text_area_description}", "")
            layout_description += " Ensure ample space for the visual."

    augmented_style = style_description # This already contains the custom prompt if applicable
    if 1 <= creativity_score <= 3: augmented_style += " Standard, clear, conventional slide design."
    elif 4 <= creativity_score <= 7: augmented_style += " Professional, well-composed visual. Balanced design."
    elif 8 <= creativity_score <= 10: augmented_style += " Highly creative, artistic interpretation. Apple Keynote aesthetic, cinematic lighting, dynamic composition, unique visual metaphors. High-end design."

    prompt = f"Create a complete presentation slide visual including all specified text elements, designed for a 3:2 aspect ratio (1536x1024 pixels).\n\n**Instructions:**\n1. **Visual:** Generate visual: '{visual_content_hint}'.\n2. **Title:** Include title: \"{slide_title}\".\n"
    if has_body_content:
        if slide_type == "Title": prompt += f"3. **Presenter Name:** Include the exact text: \"{body_content_text}\". Use a smaller, secondary font size.\n"
        else: text_length_hint = "short" if len(body_content_text) < 150 else "medium" if len(body_content_text) < 300 else "long"; prompt += f"3. **Body Content:** Include {content_type_description} ({text_length_hint} length):\n```\n{body_content_text}\n```\n"
    else: prompt += f"3. **Body Content:** None required for this slide. Design the layout considering only the title and visual.\n"

    prompt += f"4. **Overall Style:** Adhere strictly to: '{augmented_style}'.\n"
    prompt += f"5. **Font & Text Size:** Use font '{font_choice}' consistently. Ensure EXCELLENT readability. CRITICAL: Adjust text size appropriately (e.g., larger title, smaller body/name) so ALL content fits comfortably well within its designated area based on the layout ({layout_description}). AVOID text overflow, cutoff, or text being excessively large or small for its area. Add reasonable padding around text blocks - text MUST NOT touch the edges of its container or the slide borders.\n"
    if "pencil sketch style" in style_description.lower() or "pencil & paper" in style_description.lower(): prompt += f"   * SPECIAL INSTRUCTION FOR PENCIL STYLE: Make the text (title, body content) appear as if written neatly with a pencil or a simple handwriting font. It should look hand-drawn but legible.\n"
    prompt += f"6. **Layout & Readability:** Arrange elements harmoniously: '{layout_description}'. CRITICAL: Place ALL text (title, body, name) fully inside dedicated areas with HIGH CONTRAST against the background.\n"
    prompt += f"7. **Safe Zone & Padding:** ABSOLUTELY CRITICAL - Ensure ALL text and vital parts of the visual are placed well within the central 90-95% of the 3:2 image canvas. DO NOT place text or essential visual elements touching or extremely close to the absolute edges (top, bottom, left, right). Leave generous padding around text and away from borders.\n"
    prompt += f"8. **Colors:** Use colors consistent with style description.\n"
    if slide_number > 1: prompt += f"9. **Variety:** Use a different visual composition/layout than the previous slide, if appropriate for the content and creativity level.\n"

    max_len = 3950
    final_prompt = prompt.strip()[:max_len]
    log_prompt_to_file("Image Prompt Construction V10", {
        "Slide No": f"{slide_number}/{total_slides}", "Slide Type": slide_type, "Text Style Hint": text_style,
        "Has Body Content": has_body_content, "Creativity": creativity_score, "Font": font_choice,
        "Presenter": presenter_name_clean or "N/A", "Layout Desc": layout_description,
        "Generated Prompt (Excerpt)": final_prompt[:500] + "..."
    })
    return final_prompt


# --- generate_slide_image (No changes needed) ---
def generate_slide_image(image_prompt: str, presentation_id: int, slide_number: int) -> tuple[str | None, str]:
    """
    Generates and saves a slide image using gpt-image-1 via the images endpoint,
    handling the base64 JSON response. Uses 1536x1024 (3:2 ratio).
    """
    client = get_openai_client()
    saved_path_relative = None
    save_path_full = "[Path not determined]"
    actual_prompt_used = image_prompt
    revised_prompt = "[N/A for gpt-image-1]"

    log_prompt_to_file("Image Generation Request (gpt-image-1)", {"Prompt Sent": image_prompt})

    try:
        model_to_use = "gpt-image-1"
        current_app.logger.info(f"Generating image ({model_to_use}) for Pres:{presentation_id} Slide:{slide_number} via images endpoint...")
        response = client.images.generate(
            model=model_to_use,
            prompt=image_prompt,
            n=1,
            size="1536x1024",
            quality="high"
        )

        image_base64 = None
        if response.data and response.data[0].b64_json:
            image_base64 = response.data[0].b64_json
            current_app.logger.info(f"Base64 image data received for Pres:{presentation_id} Slide:{slide_number}.")
            log_prompt_to_file("Image Generation Response Info (gpt-image-1)", f"Pres ID: {presentation_id}, Slide: {slide_number}\nResponse format: b64_json (Default)")
        else:
            current_app.logger.error(f"OpenAI response missing image data (b64_json) for Pres:{presentation_id} Slide:{slide_number}. Response: {response}")
            raise ValueError("API response did not contain expected b64_json image data.")

        if image_base64:
            try: image_bytes = base64.b64decode(image_base64)
            except (TypeError, base64.binascii.Error) as decode_error:
                current_app.logger.error(f"Base64 Decode Error for Pres:{presentation_id} Slide:{slide_number}: {decode_error}")
                raise ValueError("Failed to decode base64 image data.") from decode_error

            if not os.path.exists(current_app.static_folder): os.makedirs(current_app.static_folder)
            base_upload_folder = os.path.join(current_app.static_folder, 'uploads')
            presentation_folder = os.path.join(base_upload_folder, str(presentation_id))
            os.makedirs(presentation_folder, exist_ok=True)

            unique_id = uuid.uuid4().hex[:8]
            filename = f"slide_{slide_number}_{unique_id}.png"
            save_path_full = os.path.join(presentation_folder, filename)
            current_app.logger.info(f"Attempting to save decoded image bytes to: {save_path_full}")

            try:
                with open(save_path_full, 'wb') as f: f.write(image_bytes)
                bytes_written = len(image_bytes)
                if os.path.exists(save_path_full) and bytes_written > 0:
                    saved_path_relative = os.path.join('uploads', str(presentation_id), filename).replace(os.path.sep, '/')
                    current_app.logger.info(f"Image successfully saved ({bytes_written} bytes) for Pres:{presentation_id} Slide:{slide_number} as: {saved_path_relative}")
                else:
                    current_app.logger.error(f"Image file NOT found or empty after write attempt at {save_path_full}")
                    saved_path_relative = None
            except IOError as e:
                current_app.logger.error(f"Error saving image file for Pres:{presentation_id} Slide:{slide_number} to path {save_path_full}: {e}")
                saved_path_relative = None
        else:
             current_app.logger.error(f"Image base64 data was unexpectedly None for Pres:{presentation_id} Slide:{slide_number}")
             saved_path_relative = None

    except RateLimitError as e:
        current_app.logger.error(f"OpenAI Rate Limit Error ({model_to_use}) for Pres:{presentation_id} Slide:{slide_number}: {e}")
        raise
    except APIConnectionError as e:
        current_app.logger.error(f"OpenAI Connection Error ({model_to_use}) for Pres:{presentation_id} Slide:{slide_number}: {e}")
        raise
    except APIStatusError as e:
         current_app.logger.error(f"OpenAI API Status Error ({model_to_use}) for Pres:{presentation_id} Slide:{slide_number}: Status={e.status_code}, Response={e.response}")
         if e.status_code == 404: current_app.logger.error(f"Model '{model_to_use}' not found or access denied.")
         elif e.status_code == 400: current_app.logger.error(f"Bad Request (400) calling {model_to_use}. Check API parameters or prompt content. Details: {e.response}")
         raise
    except OpenAIError as e:
        current_app.logger.error(f"OpenAI API Error ({model_to_use}) for Pres:{presentation_id} Slide:{slide_number}: {e}")
        raise
    except (IOError, ValueError, TypeError, base64.binascii.Error) as e:
        current_app.logger.error(f"File/Decode Error during image processing for slide {slide_number}: {e}", exc_info=True)
        saved_path_relative = None
    except Exception as e:
        current_app.logger.error(f"Unexpected error in generate_slide_image ({model_to_use}) for Pres:{presentation_id} Slide:{slide_number}: {e}", exc_info=True)
        saved_path_relative = None

    return saved_path_relative, image_prompt
