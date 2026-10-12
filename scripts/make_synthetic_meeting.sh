#!/bin/bash
# Generates a synthetic two-speaker "meeting" recording to check the pipeline. No real data.
# Needs macOS (the `say` command and its voices) and ffmpeg.
#
#   scripts/make_synthetic_meeting.sh [--lang ru|en] [dir]     dir defaults to ~/transcribator-synthetic
#
# Output:
#   --lang ru (default): synthetic-2speakers.mp3 and synthetic-2speakers.reference.txt
#   --lang en:           synthetic-2speakers-en.mp3 and synthetic-2speakers-en.reference.txt
# The reference text is for `transcribator wer`.
#
# Russian uses one voice (Milena); the second "speaker" is the same voice pitched down, so
# diarization on it is hard: this is a smoke test, not a quality benchmark. English uses two
# different voices (Samantha and Daniel by default, with fallbacks); if only one English
# voice is installed, the second speaker is pitch-shifted the same way.
set -euo pipefail

usage() {
    echo "usage: scripts/make_synthetic_meeting.sh [--lang ru|en] [dir]" >&2
}

lang="ru"
out=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --lang)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            lang="$2"
            shift 2
            ;;
        --lang=*)
            lang="${1#--lang=}"
            shift
            ;;
        -h | --help) usage; exit 0 ;;
        -*) usage; exit 2 ;;
        *)
            [ -z "$out" ] || { usage; exit 2; }
            out="$1"
            shift
            ;;
    esac
done
out="${out:-$HOME/transcribator-synthetic}"

# voice_exists NAME: true if `say` knows this voice.
voice_exists() {
    say -v "$1" '' >/dev/null 2>&1
}

# pick_voice EXCLUDE CANDIDATE...: prints the first installed candidate other than EXCLUDE.
pick_voice() {
    local exclude="$1" v
    shift
    for v in "$@"; do
        if [ "$v" != "$exclude" ] && voice_exists "$v"; then
            echo "$v"
            return 0
        fi
    done
    return 1
}

case "$lang" in
    ru)
        name="synthetic-2speakers"
        voice_a="Milena"
        voice_exists "$voice_a" || { echo "Voice Milena not found (see: say -v '?')" >&2; exit 1; }
        voice_b="$voice_a"
        # Alternating lines: A, B, A, B...
        lines=(
          "A|Всем привет. Давайте начнём встречу и обсудим план релиза на следующую неделю."
          "B|Привет. У меня два вопроса. Первый про миграцию базы данных, второй про тестирование."
          "A|Хорошо. По миграции мы договорились сделать её в среду вечером, когда нагрузка минимальная."
          "B|Понял. Тогда мне нужно ещё два дня, чтобы проверить интеграцию с платёжным сервисом."
          "A|Это нормально. Перенесём релиз на четверг, а в пятницу сделаем разбор результатов."
          "B|Отлично. Я подготовлю список задач и отправлю его всем до конца дня."
        )
        ;;
    en)
        name="synthetic-2speakers-en"
        voice_a="$(pick_voice "" Samantha Alex Fred Karen Moira Tessa Daniel)" || {
            echo "No English voice found (see: say -v '?'); install one in System Settings > Accessibility > Spoken Content." >&2
            exit 1
        }
        voice_b="$(pick_voice "$voice_a" Daniel Fred Karen Moira Tessa Alex Samantha)" || voice_b="$voice_a"
        if [ "$voice_b" = "$voice_a" ]; then
            echo "Only one English voice ($voice_a) found; the second speaker will be pitch-shifted." >&2
        fi
        lines=(
          "A|Hi everyone. Let's start the meeting and go over the release plan for next week."
          "B|Hi. I have two questions. The first is about the database migration, the second is about testing."
          "A|Okay. For the migration, we agreed to run it on Wednesday evening, when the load is lowest."
          "B|Got it. Then I need two more days to check the integration with the payment service."
          "A|That's fine. Let's move the release to Thursday and review the results on Friday."
          "B|Great. I'll put together the task list and send it to everyone by the end of the day."
        )
        ;;
    *)
        echo "Unknown language: $lang (expected ru or en)" >&2
        exit 2
        ;;
esac

mkdir -p "$out"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

reference="$out/$name.reference.txt"
: > "$work/list.txt"
: > "$reference"
i=0
for entry in "${lines[@]}"; do
  speaker="${entry%%|*}"
  text="${entry#*|}"
  i=$((i + 1))
  if [ "$speaker" = "B" ]; then voice="$voice_b"; else voice="$voice_a"; fi
  say -v "$voice" -r 175 --data-format=LEI16@22050 -o "$work/s$i.wav" "$text"
  if [ "$speaker" = "B" ] && [ "$voice_b" = "$voice_a" ]; then
    # Same voice for both speakers: pitch B down. 18742/22050 = 0.85, atempo = 1/0.85 restores the tempo.
    ffmpeg -y -v error -i "$work/s$i.wav" -af "asetrate=18742,aresample=22050,atempo=1.1765" "$work/p$i.wav"
  else
    cp "$work/s$i.wav" "$work/p$i.wav"
  fi
  echo "file '$work/p$i.wav'" >> "$work/list.txt"
  # Short silence between lines.
  ffmpeg -y -v error -f lavfi -i "anullsrc=r=22050:cl=mono" -t 0.6 "$work/gap$i.wav"
  echo "file '$work/gap$i.wav'" >> "$work/list.txt"
  printf '%s ' "$text" >> "$reference"
done

ffmpeg -y -v error -f concat -safe 0 -i "$work/list.txt" -ac 1 -ar 16000 -b:a 64k "$out/$name.mp3"
echo "Done: $out/$name.mp3"
