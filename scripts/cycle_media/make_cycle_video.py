"""19.09.2026: ролик-схема цикла DoDick (стадион с бегуном) — кадры PNG 1200x630, 14 с, 25 к/с.
Нужно: pip install cairosvg; шрифты IBM Plex Sans (SemiBold, Bold) и IBM Plex Mono установлены в системе.
Запуск: python make_cycle_video.py  → папка frames/ рядом со скриптом.
Склейка: ffmpeg -framerate 25 -i frames/%04d.png -c:v libx264 -pix_fmt yuv420p -crf 23 -movflags +faststart -an dodick-cycle.mp4
Под описание бота (BotFather): ... -vf "scale=960:-2,pad=960:540:(ow-iw)/2:(oh-ih)/2:color=0xF7F6F2" ... dodick-cycle-960x540.mp4
Та же схема на сайте: deploy/www/index.html и deploy/www/cycle/index.html — при правке шагов менять во всех трёх местах."""
import cairosvg, math, os
AI='fill="#ECEBFB" stroke="#2E2BD6"'; ME='fill="#fff" stroke="#15181D"'
T='font-family="IBM Plex Sans" font-weight="600" font-size="14" fill="#15181D"'
S='font-family="IBM Plex Mono" font-size="12" fill="#6E7480"'
trk="M230,110 H450 A100,100 0 0 1 450,310 H230 A100,100 0 0 1 230,110 Z"
L=440+2*math.pi*100
at=[0,110,220,377,534,644,754,911]
tips=["Тренер публикует анонс — ИИ раскладывает тренировку по группам",
 "Бот сам берёт данные с часов: зоны, нагрузку, восстановление",
 "Вечером накануне — какая группа подходит и насколько, в %",
 "Утром бот смотрит сон, HRV и готовность",
 "Вы бежите свою группу — или выбираете другую",
 "Разбор: факт против плана и чей выбор группы был верным",
 "Вы оцениваете разбор от 1 до 10",
 "По результатам всё калибруется — следующий совет точнее"]
def pos(d):
    d%=L
    if d<220: return 230+d,110,1
    d-=220
    if d<math.pi*100: a=-math.pi/2+d/100; return 450+100*math.cos(a),210+100*math.sin(a),1
    d-=math.pi*100
    if d<220: return 450-d,310,-1
    d-=220; a=math.pi/2+d/100; return 230+100*math.cos(a),210+100*math.sin(a),-1
def facing(d):
    x1,y1,_=pos(d); x2,y2,_=pos(d+2); return -1 if x2<x1-0.05 else 1
def ease(u): return 0.5-0.5*math.cos(math.pi*u)
def osc(t,per):  # alternate 0..1..0
    u=(t%(2*per))/per; return ease(u) if u<=1 else ease(2-u)
def lerp(a,b,u): return a+(b-a)*u
def leg(th,sh): return f'<g transform="translate(0,-9) rotate({th:.1f})"><line x1="0" y1="0" x2="0" y2="7"/><g transform="translate(0,7) rotate({sh:.1f})"><line x1="0" y1="0" x2="0" y2="7"/></g></g>'
def arm(a): return f'<g transform="translate(4,-19) rotate({a:.1f})"><polyline points="0,0 0,5 3,8"/></g>'
def sw(k,on): return 2.6 if k==on else 1.3
def frame(t,D):
    d=(t/D)*L; on=-1
    for j,a in enumerate(at):
        if abs(((d-a+L/2)%L)-L/2)<40: on=j
    x,y,_=pos(d); f=facing(d)
    u=osc(t,0.34)
    pr=(t%1.6)/1.6; pr_r=18+16*pr; pr_o=0.6*(1-pr)
    rot=(t%6)/6*360
    s=f'''<path d="{trk}" fill="none" stroke="#D9D6CC" stroke-width="14"/>
<path d="{trk}" fill="none" stroke="#fff" stroke-width="1" stroke-dasharray="6 6"/>
<text {S} x="340" y="204" text-anchor="middle">каждая тренировка —</text>
<text {S} x="340" y="222" text-anchor="middle">новый круг</text>
<circle cx="230" cy="110" r="{pr_r:.1f}" fill="none" stroke="#2E2BD6" stroke-width="1" opacity="{pr_o:.2f}"/>
<polygon {AI} stroke-width="{sw(0,on)}" points="230,90 247,100 247,120 230,130 213,120 213,100"/>
<text {T} x="230" y="62" text-anchor="middle">Анонс → ИИ</text><text {S} x="230" y="80" text-anchor="middle">раскладка по группам</text>
<rect {AI} stroke-width="{sw(1,on)}" x="329" y="97" width="22" height="26" rx="6"/>
<line x1="335" y1="90" x2="345" y2="90" stroke="#2E2BD6" stroke-width="2"/><line x1="335" y1="130" x2="345" y2="130" stroke="#2E2BD6" stroke-width="2"/>
<polyline points="332,110 336,110 338,105 341,115 343,110 348,110" fill="none" stroke="#2E2BD6" stroke-width="1.2"/>
<line x1="340" y1="50" x2="340" y2="84" stroke="#9A9EA6" stroke-width="0.8" stroke-dasharray="3 3"/>
<text {T} x="340" y="22" text-anchor="middle">Данные с часов</text><text {S} x="340" y="40" text-anchor="middle">зоны, нагрузка, сон</text>
<rect {AI} stroke-width="{sw(2,on)}" x="432" y="94" width="36" height="32" rx="4"/>
<rect x="439" y="112" width="5" height="9" fill="#2E2BD6"/><rect x="447" y="104" width="5" height="17" fill="#2E2BD6"/><rect x="455" y="108" width="5" height="13" fill="#2E2BD6"/>
<text {T} x="450" y="62" text-anchor="middle">Рекомендация</text><text {S} x="450" y="80" text-anchor="middle">группа и %</text>
<circle {ME} stroke-width="{sw(3,on)}" cx="550" cy="210" r="17"/>
<text {T} x="578" y="206">Утро</text><text {S} x="578" y="224">сон и HRV</text>
<rect {ME} stroke-width="{sw(4,on)}" x="430" y="298" width="40" height="24" rx="12"/>
<text {T} x="450" y="348" text-anchor="middle">Тренировка</text><text {S} x="450" y="366" text-anchor="middle">своя группа</text>
<rect {AI} stroke-width="{sw(5,on)}" x="322" y="294" width="36" height="32" rx="4"/>
<polyline points="327,318 334,309 341,313 348,302 353,306" fill="none" stroke="#2E2BD6" stroke-width="1.5"/>
<text {T} x="340" y="348" text-anchor="middle">Разбор</text><text {S} x="340" y="366" text-anchor="middle">кто был прав</text>
<polygon {ME} stroke-width="{sw(6,on)}" points="230,292 235,304 248,304 238,312 242,325 230,317 218,325 222,312 212,304 225,304"/>
<text {T} x="230" y="348" text-anchor="middle">Оценка</text><text {S} x="230" y="366" text-anchor="middle">от 1 до 10</text>
<circle {AI} stroke-width="{sw(7,on)}" cx="130" cy="210" r="17"/>
<g transform="rotate({rot:.1f} 130 210)" stroke="#2E2BD6" stroke-width="2"><line x1="130" y1="196" x2="130" y2="201"/><line x1="130" y1="219" x2="130" y2="224"/><line x1="116" y1="210" x2="121" y2="210"/><line x1="139" y1="210" x2="144" y2="210"/></g>
<text {T} x="102" y="206" text-anchor="end">Калибровка</text><text {S} x="102" y="224" text-anchor="end">всё точнее</text>
<g transform="translate({x:.1f},{y-3:.1f}) scale({1.4*f},1.4)" stroke="#E4572E" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" fill="none">
{leg(lerp(35,-45,u),lerp(80,15,u))}{arm(lerp(40,-45,1-u))}<line x1="0" y1="-9" x2="4" y2="-19"/><circle cx="6.5" cy="-24" r="3.8" fill="#E4572E" stroke="none"/>{leg(lerp(-45,35,u),lerp(15,80,u))}{arm(lerp(40,-45,u))}
</g>'''
    order=sorted(range(len(at)),key=lambda j:at[j]); last=[j for j in order if at[j]<=d+40][-1]
    tip=tips[last]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
<rect width="1200" height="630" fill="#F7F6F2"/>
<text x="60" y="78" font-family="IBM Plex Sans" font-weight="700" font-size="40" fill="#15181D">Do<tspan fill="#2E2BD6">Dick</tspan> — как это работает</text>
<text x="1140" y="76" text-anchor="end" font-family="IBM Plex Mono" font-size="22" fill="#2E2BD6">@DD_adviser_bot · dodick.run</text>
<rect x="60" y="108" width="1080" height="492" fill="#fff" stroke="#E3E0D8"/>
<g transform="translate(209,114) scale(1.15)">{s}</g>
<text x="600" y="578" text-anchor="middle" font-family="IBM Plex Sans" font-size="21" fill="#2A2F38">{tip}</text>
</svg>'''
D=14.0; FPS=25; OUT=os.path.join(os.path.dirname(os.path.abspath(__file__)),'frames'); os.makedirs(OUT,exist_ok=True)
for i in range(int(D*FPS)):
    cairosvg.svg2png(bytestring=frame(i/FPS,D).encode(),write_to=os.path.join(OUT,f'{i:04d}.png'))
print("frames ok")
