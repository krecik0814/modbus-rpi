// Widok: Pomoc - poradnik po polsku ze spisem treści i tabelą obsługiwanych liczników (z /api/presets).
// Treść sekcji to stałe, zaufane fragmenty HTML napisane tutaj; dane z API trafiają do DOM wyłącznie przez h()/textContent.

import { h, mount as fill, get, pageHeader } from '../core.js';

// ── treść (stałe HTML) ───────────────────────────────────────

const START = `
<ol>
  <li><b>Instalacja:</b> <code>pip install -r requirements.txt</code>. Na Raspberry Pi najwygodniej uruchomić
    <code>bash deploy/install-rpi.sh</code> - skrypt tworzy środowisko <code>.venv</code>, dodaje użytkownika do grupy
    <code>dialout</code> i instaluje usługę systemd.</li>
  <li><b>Próba bez sprzętu:</b> <code>python app.py</code> uruchamia wbudowany symulator licznika 3-fazowego
    (Modbus TCP, port 5020) i panel pod adresem <code>http://localhost:5000</code>. Symulator od razu widać na
    <a href="#dashboard">Dashboardzie</a> jako urządzenie <i>Symulator 3F</i>.</li>
  <li><b>Prawdziwy licznik:</b> podłącz go do magistrali RS-485 (opis niżej) i uruchom np.
    <code>python app.py --serial /dev/serial0 --baudrate 9600 --preset eastron_sdm630 --unit 1</code>.
    Zamiast flag możesz dodać połączenie w zakładce <a href="#connections">Połączenia</a>, a licznik
    w zakładce <a href="#devices">Urządzenia</a>.</li>
  <li><b>Nie znasz parametrów licznika?</b> W <a href="#scanner">Skanerze rejestrów</a> użyj
    <i>Szukaj urządzeń (Unit ID)</i>, a w formularzu urządzenia przycisku <i>Rozpoznaj licznik</i> - aplikacja
    porówna odczyty z biblioteką presetów.</li>
  <li><b>Licznika nie ma w bibliotece?</b> Zeskanuj rejestry, kliknij <i>Utwórz preset</i>, popraw nazwy i jednostki,
    zapisz. Preset działa potem dla każdego egzemplarza tego modelu - zmienia się tylko Unit ID.</li>
  <li><b>Integracje:</b> MQTT i Home Assistant, historia w SQLite oraz Prometheus - zakładka
    <a href="#integrations">Integracje</a>.</li>
</ol>
<p>Praca w tle po starcie systemu: <code>sudo systemctl enable --now modbus-dash</code>, logi:
<code>journalctl -u modbus-dash -f</code>. Parametry uruchomienia zmienisz w linii <code>ExecStart</code> pliku
<code>/etc/systemd/system/modbus-dash.service</code>.</p>`;

const RS485 = `
<p>RS-485 to dwuprzewodowa magistrala różnicowa. Raspberry Pi łączy się z nią przez nakładkę (HAT) z układem RS-485
albo przejściówkę USB-RS485. Licznik ma zaciski opisane zwykle <b>A</b>, <b>B</b> i czasem <b>GND</b> / <b>COM</b>.</p>
<pre class="code">Licznik             Nakładka / adapter RS-485
  A (D+)  ---------  A (D+)
  B (D-)  ---------  B (D-)
  GND     - - - - -  GND      (zalecane przy dłuższych przewodach)</pre>
<ul>
  <li><b>A do A, B do B.</b> Producenci różnie oznaczają linie (u niektórych A to D-), więc gdy licznik nie odpowiada,
    po prostu zamień przewody - nic się nie uszkodzi.</li>
  <li><b>Skrętka:</b> A i B poprowadź jedną parą skrętki (np. z kabla UTP). <b>GND</b> (masa odniesienia) połącz
    trzecim przewodem, szczególnie przy długiej magistrali albo urządzeniach zasilanych z różnych źródeł.</li>
  <li><b>Topologia liniowa:</b> licznik za licznikiem (łańcuch), bez gwiazdy i długich odgałęzień.</li>
  <li><b>Terminacja 120 Ω:</b> rezystor między A i B tylko na <b>obu końcach</b> magistrali (wiele nakładek ma zworkę
    lub przełącznik). Przy jednym liczniku na kilku metrach zwykle niepotrzebna; przy długiej linii jej brak daje
    losowe błędy CRC.</li>
  <li><b>Do 32 urządzeń</b> o standardowym obciążeniu na jednym segmencie (więcej z repeaterem albo układami 1/4, 1/8
    obciążenia).</li>
  <li><b>Do 1200 m</b> przy niskich prędkościach, np. 9600 bit/s. Im wyższa prędkość, tym krótszy może być kabel.</li>
  <li>Wszystkie liczniki na jednej magistrali muszą mieć <b>te same parametry portu</b> (prędkość, parzystość)
    i <b>różne Unit ID</b>.</li>
  <li>Nakładki z automatycznym sterowaniem kierunkiem (np. Waveshare RS485 CAN HAT) i przejściówki USB (FTDI, CH340,
    CP210x) działają bez konfiguracji. Nakładki wymagające sterowania pinem DE/RE z GPIO nie są obsługiwane.</li>
  <li><b>Bezpieczeństwo:</b> liczniki są w rozdzielnicy pod napięciem sieciowym. Przewody podłączaj przy wyłączonym
    zasilaniu, a prace w rozdzielnicy zleć osobie z uprawnieniami.</li>
</ul>`;

const UART = `
<p>Nakładki RS-485 korzystają z UART na pinach GPIO14 (TX) i GPIO15 (RX). Najpierw włącz port i wyłącz na nim konsolę:</p>
<pre class="code">sudo raspi-config
# Interface Options -> Serial Port
#   Would you like a login shell to be accessible over serial?  -> No
#   Would you like the serial port hardware to be enabled?      -> Yes
sudo reboot
ls -l /dev/serial0        # sprawdź, na który port wskazuje alias</pre>
<ul>
  <li><b><code>/dev/serial0</code></b> to alias na UART pinów GPIO, niezależny od modelu - używaj go w
    <code>--serial</code> i w zakładce Połączenia.</li>
  <li><b>RPi 3, 4, Zero W, Zero 2 W:</b> pełny UART (PL011, <code>/dev/ttyAMA0</code>) obsługuje domyślnie Bluetooth,
    a <code>serial0</code> wskazuje na mini-UART (<code>/dev/ttyS0</code>). <b>Mini-UART nie obsługuje parzystości
    ani 2 bitów stopu</b> (jądro po cichu przełącza go na 8N1), więc liczniki 8E1 (np. Orno) nie odpowiedzą - aplikacja
    wykrywa to i zgłasza od razu. Jego prędkość zależy też od taktowania procesora. Dopisz do <code>/boot/firmware/config.txt</code>
    (w systemach starszych niż Bookworm: <code>/boot/config.txt</code>):
    <pre class="code">dtoverlay=disable-bt</pre>
    wykonaj <code>sudo systemctl disable hciuart</code> i uruchom ponownie. Bluetooth zostanie wyłączony,
    a <code>serial0</code> wskaże <code>ttyAMA0</code>.</li>
  <li><b>RPi 5:</b> UART na GPIO14/15 to <code>/dev/ttyAMA0</code>; Bluetooth ma osobny interfejs, więc
    <code>disable-bt</code> nie jest potrzebne. <code>/dev/ttyAMA10</code> to osobne 3-pinowe złącze debug UART
    (między portami micro-HDMI) - nie podłączaj do niego RS-485. Jeśli <code>serial0</code> wskazuje na
    <code>ttyAMA10</code>, podaj wprost <code>--serial /dev/ttyAMA0</code>; gdy <code>ttyAMA0</code> nie istnieje,
    dopisz <code>dtparam=uart0=on</code> do <code>/boot/firmware/config.txt</code>.</li>
  <li><b>Uprawnienia:</b> użytkownik musi należeć do grupy <code>dialout</code>:
    <code>sudo usermod -a -G dialout $USER</code>, potem wyloguj się i zaloguj ponownie (sprawdzenie:
    <code>groups</code>). Usługa systemd ma <code>Group=dialout</code>.</li>
  <li><b>Przejściówka USB:</b> pojawia się jako <code>/dev/ttyUSB0</code> (CH340, FTDI, CP210x) albo
    <code>/dev/ttyACM0</code>. Nazwa odporna na kolejność podłączania: <code>/dev/serial/by-id/...</code>.
    Wykryte porty widać w zakładce <a href="#connections">Połączenia</a>.</li>
</ul>`;

const PARAMS = `
<p>Parametry ustawisz w zakładce <a href="#connections">Połączenia</a> albo flagami CLI (wtedy połączenie
<code>default</code> jest w panelu zablokowane do edycji). Licznik i aplikacja muszą mieć identyczne ustawienia portu.</p>
<div class="table-wrap"><table class="tbl">
  <thead><tr><th scope="col">Parametr</th><th scope="col">Opis</th><th scope="col">Typowo</th></tr></thead>
  <tbody>
    <tr><td>Prędkość (baudrate)</td><td>szybkość transmisji w bit/s</td><td>9600; Eastron SDM120: 2400; zakres 1200-115200</td></tr>
    <tr><td>Parzystość</td><td>N (brak), E (parzysta), O (nieparzysta)</td><td>N; Orno, Inepro, WAGO: E</td></tr>
    <tr><td>Bity danych i stopu</td><td>zapis skrócony: 8N1 = 8 bitów danych, brak parzystości, 1 bit stopu</td><td>8N1, 8E1; rzadziej 8N2</td></tr>
    <tr><td>Unit ID (adres slave)</td><td>adres licznika na magistrali, 1-247, każdy licznik inny; 0 to adres rozgłoszeniowy (bez odpowiedzi)</td><td>1</td></tr>
    <tr><td>Timeout</td><td>jak długo czekać na odpowiedź</td><td>1 s; bramki i wolne liczniki 2-3 s</td></tr>
    <tr><td>Ponowienia</td><td>ile razy powtórzyć zapytanie bez odpowiedzi</td><td>1</td></tr>
    <tr><td>Przerwa między ramkami</td><td>dodatkowa pauza przed kolejnym zapytaniem; pomaga wolnym licznikom i bramkom</td><td>0 ms; przy błędach 20-100 ms</td></tr>
    <tr><td>Host i port TCP</td><td>licznik z Ethernetem albo bramka Modbus TCP</td><td>port 502</td></tr>
    <tr><td>RTU over TCP</td><td>bramka transparentna (np. USR-TCP232, Elfin EW11) przesyła surowe ramki RTU przez TCP - wybierz ten typ zamiast Modbus TCP</td><td>port bramki, np. 8899</td></tr>
  </tbody>
</table></div>`;

const FUNCTIONS_HTML = `
<p>Modbus udostępnia cztery tablice danych. Liczniki trzymają pomiary w rejestrach 16-bitowych - <b>Input</b> albo
<b>Holding</b>, zależnie od producenta. Jeśli licznik odpowiada wyjątkiem 01, spróbuj drugiego typu.</p>
<div class="table-wrap"><table class="tbl">
  <thead><tr><th scope="col">Kod</th><th scope="col">Funkcja</th><th scope="col">Zastosowanie</th></tr></thead>
  <tbody>
    <tr><td>FC01</td><td>Read Coils - odczyt bitów (cewek)</td><td>stany wyjść, przekaźniki</td></tr>
    <tr><td>FC02</td><td>Read Discrete Inputs - odczyt bitów wejściowych</td><td>wejścia, alarmy</td></tr>
    <tr><td>FC03</td><td>Read Holding Registers - rejestry 16-bit do odczytu i zapisu</td><td>konfiguracja; w wielu licznikach (Orno, ABB, Chint, Schneider) także pomiary</td></tr>
    <tr><td>FC04</td><td>Read Input Registers - rejestry 16-bit tylko do odczytu</td><td>pomiary: Eastron, Carlo Gavazzi, Finder i inne</td></tr>
    <tr><td>FC05</td><td>Write Single Coil - zapis jednego bitu</td><td>sterowanie przekaźnikiem</td></tr>
    <tr><td>FC06</td><td>Write Single Register - zapis jednego rejestru</td><td>proste ustawienia</td></tr>
    <tr><td>FC15</td><td>Write Multiple Coils - zapis wielu bitów</td><td>sterowanie wieloma wyjściami</td></tr>
    <tr><td>FC16</td><td>Write Multiple Registers - zapis wielu rejestrów</td><td>ustawienia 32-bit, np. Eastron (float32) zmienia się tylko przez FC16</td></tr>
    <tr><td>FC43</td><td>Read Device Identification (MEI 0x0E)</td><td>producent, model, wersja - obsługuje tylko część liczników</td></tr>
  </tbody>
</table></div>
<p>Zapis (FC05, FC06, FC15, FC16) jest wyłączony, dopóki nie uruchomisz aplikacji z <code>--allow-write</code>.
Zmiana Unit ID albo prędkości licznika przez zapis może go "zgubić" - zanotuj nowe wartości.</p>
<p>Kody wyjątków w odpowiedzi: <b>01</b> niedozwolona funkcja, <b>02</b> niedozwolony adres, <b>03</b> niedozwolona
wartość (np. za dużo rejestrów naraz), <b>04</b> błąd urządzenia, <b>06</b> urządzenie zajęte, <b>0A</b>/<b>0B</b>
błąd bramki (brak ścieżki lub brak odpowiedzi urządzenia za bramką).</p>`;

const TYPES = `
<p>Rejestr Modbus ma 16 bitów. Wartości 32-bitowe zajmują dwa kolejne rejestry, 64-bitowe cztery. Obsługiwane typy:
<code>int8</code>, <code>uint8</code> (połowa rejestru), <code>int16</code>, <code>uint16</code>, <code>int32</code>,
<code>uint32</code>, <code>float32</code>, <code>int64</code>, <code>uint64</code>, <code>float64</code>.</p>
<p>Standard nie określa, w jakiej kolejności producent układa bajty w kilku rejestrach. Przykład: <b>230,0 V</b>
jako <code>float32</code> to bajty <code>43 66 00 00</code> (oznaczane A B C D):</p>
<div class="table-wrap"><table class="tbl">
  <thead><tr><th scope="col">Kolejność</th><th scope="col">Rejestr N</th><th scope="col">Rejestr N+1</th><th scope="col">Odczytane jako ABCD</th><th scope="col">Spotykana</th></tr></thead>
  <tbody>
    <tr><td><b>ABCD</b> (big-endian)</td><td class="num">0x4366</td><td class="num">0x0000</td><td class="num">230,0</td><td>najczęstsza: Eastron, Orno, Finder, większość liczników</td></tr>
    <tr><td><b>CDAB</b> (zamiana słów, word swap)</td><td class="num">0x0000</td><td class="num">0x4366</td><td class="num">2,4e-41</td><td>Carlo Gavazzi (int32 z młodszym słowem pierwszym), część bramek i sterowników PLC</td></tr>
    <tr><td><b>BADC</b> (zamiana bajtów)</td><td class="num">0x6643</td><td class="num">0x0000</td><td class="num">2,3e+23</td><td>rzadko</td></tr>
    <tr><td><b>DCBA</b> (little-endian)</td><td class="num">0x0000</td><td class="num">0x6643</td><td class="num">3,7e-41</td><td>rzadko</td></tr>
  </tbody>
</table></div>
<p>Zła kolejność daje wartości absurdalnie małe (1e-38, 2e-41), ogromne (1e+23, 3e+38), ujemne albo NaN.
W skanerze porównaj kolumny ABCD, CDAB, BADC i DCBA - poprawna jest ta, w której napięcie wygląda na ok. 230 V.
W presecie ustaw <code>"byte_order": "CDAB"</code> (dla całego presetu albo pojedynczego rejestru).</p>
<h4>Skala i przesunięcie</h4>
<p>Wiele liczników podaje liczby całkowite w ustalonych jednostkach, np. napięcie w 0,1 V, a energię w 0,01 kWh.
Wartość fizyczna = surowa × <code>scale</code> + <code>offset</code>:</p>
<pre class="code">"voltage_l1":    {"address": 0,   "type": "uint16", "scale": 0.1,  "unit": "V"}     2301    -> 230,1 V
"energy_import": {"address": 256, "type": "uint32", "scale": 0.01, "unit": "kWh"}   1234567 -> 12345,67 kWh</pre>
<p><code>offset</code> przydaje się rzadko (np. temperatura przesunięta o -40). Pole <code>"invalid": [65535]</code>
oznacza surowe wartości, które licznik zwraca zamiast "brak danych".</p>
<h4>Skala z innego rejestru (SunSpec, Gossen EnergyMID)</h4>
<p>Niektóre urządzenia podają mantysę i osobny rejestr z wykładnikiem (tzw. scale factor). Pole
<code>"scale_from": "klucz"</code> mnoży wartość przez 10<sup>wartość wskazanego rejestru</sup>:</p>
<pre class="code">"voltage_l1": {"address": 4,  "type": "int16", "scale_from": "u_exp", "unit": "V"}
"u_exp":      {"address": 12, "type": "int8"}            mantysa 2309, wykładnik -1 -> 230,9 V</pre>
<p>Z <code>"scale_from_mode": "multiply"</code> mnożnikiem jest sama wartość rejestru (np. współczynnik energii).</p>`;

const ADDRESSES = `
<p>W ramce Modbus adres to liczba 0-65535 liczona <b>od zera</b>. Dokumentacje liczników używają jednak różnych
zapisów, więc ten sam rejestr może występować jako:</p>
<div class="table-wrap"><table class="tbl">
  <thead><tr><th scope="col">Zapis w dokumentacji</th><th scope="col">Przykład</th><th scope="col">Adres w protokole i presecie</th></tr></thead>
  <tbody>
    <tr><td>adres protokołu (0-based, często hex)</td><td><code>0x0000</code> lub <code>0</code></td><td>0</td></tr>
    <tr><td>notacja Modicon, Input Registers</td><td><code>30001</code> (także 300001)</td><td>0</td></tr>
    <tr><td>notacja Modicon, Holding Registers</td><td><code>40001</code> (także 400001)</td><td>0</td></tr>
    <tr><td>numer rejestru liczony od 1</td><td>rejestr 1</td><td>0</td></tr>
  </tbody>
</table></div>
<ul>
  <li>W Modbus Dash adresy w presetach są zawsze <b>0-based</b> (adres protokołu). Pola adresu w skanerze przyjmują też
    zapis <code>30001</code>/<code>40001</code> i liczby hex (<code>0x0156</code>) i przeliczają je same.</li>
  <li>Gdy przepisujesz mapę z dokumentacji numerowanej od 1, nie przeliczaj każdego adresu - dodaj do presetu
    <code>"address_offset": -1</code> i wpisuj adresy tak jak w dokumentacji.</li>
  <li>Objaw pomyłki o 1: zamiast 230 V widać bzdury, a poprawne wartości pojawiają się w skanerze (krok co 1 rejestr)
    pod adresem o 1 większym lub mniejszym.</li>
</ul>`;

const SCANNER = `
<p><a href="#scanner">Skaner</a> czyta zakres adresów i każdy rejestr (krok co 1) albo parę rejestrów (krok co 2,
domyślnie) pokazuje w wielu interpretacjach: surowe <code>uint16</code>/<code>int16</code> i hex, <code>float32</code>
w czterech kolejnościach bajtów oraz <code>int32</code>/<code>uint32</code>. Kolumna podpowiedzi pokazuje, na co
wygląda wartość:</p>
<div class="table-wrap"><table class="tbl">
  <thead><tr><th scope="col">Podpowiedź</th><th scope="col">Zakres</th><th scope="col">Uwagi</th></tr></thead>
  <tbody>
    <tr><td><span class="guess g-system">Częstotliwość</span></td><td>45-65 Hz</td><td>najpewniejsza przy 49-51 Hz (lub 59-61 Hz)</td></tr>
    <tr><td><span class="guess g-line_volt">Napięcie międzyfazowe</span></td><td>340-440 V</td><td>typowo 380-420 V</td></tr>
    <tr><td><span class="guess g-voltage">Napięcie fazowe</span></td><td>180-260 V lub 100-130 V</td><td>typowo 220-240 V; 100-130 V w sieciach 120 V</td></tr>
    <tr><td><span class="guess g-pf">cos φ</span></td><td>0,3-1,0 (wartość bezwzględna)</td><td>tylko liczby zmiennoprzecinkowe</td></tr>
    <tr><td><span class="guess g-current">Prąd</span></td><td>0,01-200 A</td><td>niejednoznaczny - wiele wielkości mieści się w tym zakresie</td></tr>
    <tr><td><span class="guess g-power">Moc</span></td><td>1-100 000 W, także ujemna</td><td>minus zwykle oznacza oddawanie energii do sieci (fotowoltaika)</td></tr>
    <tr><td><span class="guess g-thd">THD</span></td><td>0,1-40 %</td><td>łatwo pomylić z prądem</td></tr>
    <tr><td><span class="guess g-energy">Energia</span></td><td>od 0,01 kWh</td><td>licznik narastający; wartości powyżej 10 000 są pewniejsze</td></tr>
  </tbody>
</table></div>
<ul>
  <li><b>Pewność</b> rośnie, gdy wartość jest typowa (230 V zamiast 190 V) i gdy pasuje kolejność bajtów. Dla liczb
    całkowitych skaner próbuje skal 1, 0,1, 0,01 i 0,001 (np. 2301 -> 230,1 V), co obniża pewność.</li>
  <li><b>Sztuczka z czajnikiem:</b> włącz tryb <i>Live</i>, a potem duży odbiornik o znanej mocy (czajnik ok. 2 kW).
    Rejestry, które wyraźnie urosną, to prąd i moc fazy z czajnikiem; moc całkowita wzrośnie o ok. 2000 W, a energia
    zacznie przyrastać. Po wyłączeniu wartości wrócą.</li>
  <li><b>Zła kolejność bajtów:</b> w kolumnie ABCD widać 1e-38, 2e-41, 1e+23 albo NaN, a w CDAB sensowne liczby -
    wybierz CDAB. Gdy sensowne liczby pojawiają się tylko przy kroku co 1 pod nieparzystym adresem, mapa jest
    przesunięta o jeden rejestr (patrz <i>Adresy</i>).</li>
  <li>Same zera zwykle oznaczają nieużywany obszar albo brak obciążenia; 0xFFFF lub 0x8000 to często "brak danych".</li>
  <li>Z rozpoznanych wierszy przycisk <i>Utwórz preset</i> buduje szkic presetu - przed zapisaniem sprawdź nazwy,
    jednostki i skale.</li>
</ul>`;

const DETECT = `
<ul>
  <li><b>Szukaj urządzeń (Unit ID)</b> w <a href="#scanner">Skanerze</a> wysyła po jednym krótkim zapytaniu do każdego
    adresu z zakresu (domyślnie 1-247). Urządzenie, które odpowie wartością albo wyjątkiem Modbus, istnieje na
    magistrali. Przy timeoucie 0,3 s pełny zakres trwa ok. 1-2 minut.</li>
  <li>Gdy nie odpowiada żaden adres, problemem są parametry portu albo przewody, nie Unit ID: zmień prędkość
    i parzystość połączenia (kolejno 9600, 2400, 19200; N, potem E), zamień A z B i spróbuj ponownie.</li>
  <li><b>Rozpoznaj licznik</b> w formularzu urządzenia (<a href="#devices">Urządzenia</a>) czyta rejestry testowe
    presetów z biblioteki i ocenia, czy wartości mają sens (napięcie ok. 230 V, częstotliwość ok. 50 Hz). Wynik to
    lista kandydatów z oceną 0-100%. Kilka modeli może mieć tę samą mapę rejestrów - wybierz zgodny z tabliczką.</li>
  <li>Ustawienia fabryczne najczęściej: Unit ID 1, 9600 8N1 (Eastron SDM120: 2400 8N1; Orno: 9600 8E1). Wiele
    liczników pokazuje adres i prędkość w menu na wyświetlaczu - tam też można je zmienić.</li>
  <li>Kilka liczników z tym samym fabrycznym Unit ID: podłączaj je po jednym i każdemu nadaj inny adres w menu
    licznika (albo zapisem z <code>--allow-write</code>), zanim połączysz je w jedną magistralę.</li>
</ul>`;

const INTEGRATIONS = `
<h4>Historia</h4>
<p>Ostatnie odczyty (domyślnie 3600 na urządzenie, czyli ok. godzina przy odczycie co 1 s) są w pamięci w pełnej
rozdzielczości. Równolegle średnie, minima i maksima z okresów agregacji (domyślnie 1 min) trafiają do bazy SQLite
<code>history.sqlite</code> w katalogu danych i są przechowywane 30 dni. Wykresy na <a href="#dashboard">Dashboardzie</a>
same wybierają źródło, a przycisk <i>CSV</i> eksportuje wybrany zakres.</p>
<h4>MQTT i Home Assistant</h4>
<p>Po włączeniu MQTT w zakładce <a href="#integrations">Integracje</a> każdy licznik publikuje JSON ze wszystkimi
wartościami na <code>&lt;prefiks&gt;/&lt;urządzenie&gt;/state</code> (domyślnie co 10 s). Discovery Home Assistant
tworzy czujniki automatycznie, z właściwymi klasami - energia ma <code>total_increasing</code>, więc od razu nadaje się
do panelu Energia. Wymaga biblioteki <code>paho-mqtt</code> (<code>pip install paho-mqtt</code>).</p>
<h4>Prometheus i REST API</h4>
<p><code>/metrics</code> udostępnia wszystkie wartości jako <code>modbus_dash_value{device, key, unit, ...}</code> oraz
stan odczytów (<code>modbus_dash_up</code>). Gotowy fragment <code>prometheus.yml</code> i przykłady <code>curl</code>
są w zakładce Integracje. <code>/api/health</code> zwraca HTTP 503, gdy któryś licznik nie odpowiada - przydatne
w prostym monitoringu dostępności.</p>`;

const SECURITY = `
<div class="table-wrap"><table class="tbl">
  <thead><tr><th scope="col">Flaga</th><th scope="col">Działanie</th><th scope="col">Kiedy</th></tr></thead>
  <tbody>
    <tr><td><code>--auth USER:HASŁO</code></td><td>logowanie HTTP Basic do panelu i API; zamiast flagi można ustawić zmienną <code>MODBUS_DASH_AUTH</code> (np. w usłudze systemd)</td><td>zawsze, gdy panel jest dostępny w sieci</td></tr>
    <tr><td><code>--host 127.0.0.1</code></td><td>nasłuch tylko lokalnie; z innego komputera przez tunel SSH: <code>ssh -L 5000:localhost:5000 pi@raspberrypi.local</code></td><td>gdy panel ma być prywatny</td></tr>
    <tr><td><code>--allow-write</code></td><td>włącza zapis rejestrów i cewek (FC05, FC06, FC15, FC16) z interfejsu</td><td>tylko na czas konfiguracji licznika</td></tr>
    <tr><td><code>--debug</code></td><td>debugger Werkzeug pozwala wykonać dowolny kod, dlatego aplikacja wymusza wtedy nasłuch na 127.0.0.1</td><td>tylko przy programowaniu</td></tr>
  </tbody>
</table></div>
<ul>
  <li>Domyślnie panel nasłuchuje na wszystkich interfejsach (<code>0.0.0.0</code>) <b>bez hasła</b>.</li>
  <li>Hasło HTTP Basic jest przesyłane bez szyfrowania. W sieci, której nie ufasz, postaw przed panelem reverse proxy
    z HTTPS (np. Caddy, nginx) albo korzystaj z VPN (WireGuard, Tailscale).</li>
  <li>Nie wystawiaj portu panelu ani Modbus TCP bezpośrednio do Internetu - protokół Modbus nie ma żadnych zabezpieczeń.</li>
  <li>API jest chronione przed CSRF: zapytania zmieniające stan muszą mieć <code>Content-Type: application/json</code>
    i nie mogą pochodzić z obcej strony. Presetów wbudowanych nie da się nadpisać ani usunąć.</li>
  <li>Symulator nasłuchuje na wszystkich interfejsach (port 5020). Na docelowym urządzeniu używaj <code>--no-sim</code>
    albo <code>--serial</code>/<code>--tcp</code> (wtedy symulator jest domyślnie wyłączony).</li>
</ul>`;

const TROUBLE = `
<div class="table-wrap"><table class="tbl help-trouble">
  <thead><tr><th scope="col">Objaw</th><th scope="col">Przyczyna</th><th scope="col">Rozwiązanie</th></tr></thead>
  <tbody>
    <tr><td>Brak odpowiedzi (timeout)</td><td>zły Unit ID, prędkość lub parzystość</td><td>sprawdź menu licznika; <i>Szukaj urządzeń</i> w Skanerze; typowo 9600 8N1, Eastron SDM120 2400 8N1, Orno 9600 8E1</td></tr>
    <tr><td>Timeout dla każdego Unit ID</td><td>zamienione A/B, brak GND, licznik bez zasilania, zły port</td><td>zamień A z B; sprawdź <code>ls -l /dev/serial0</code>; diody TX/RX adaptera powinny migać</td></tr>
    <tr><td>Timeout na RPi, port otwiera się poprawnie</td><td>konsola na UART albo mini-UART (RPi 3/4)</td><td><code>raspi-config</code>: login shell NIE; <code>dtoverlay=disable-bt</code></td></tr>
    <tr><td><code>Permission denied</code> przy otwieraniu portu</td><td>brak uprawnień do portu</td><td><code>sudo usermod -a -G dialout $USER</code>, wyloguj się i zaloguj</td></tr>
    <tr><td><code>could not open port</code>, brak pliku</td><td>zła nazwa portu albo UART wyłączony</td><td><code>ls /dev/serial* /dev/ttyUSB*</code>; listę portów pokazuje zakładka Połączenia</td></tr>
    <tr><td>Wyjątek 01 (niedozwolona funkcja)</td><td>licznik nie obsługuje FC04 albo FC03</td><td>zmień typ rejestrów (Input/Holding) w skanerze albo <code>register_type</code> w presecie</td></tr>
    <tr><td>Wyjątek 02 (niedozwolony adres)</td><td>rejestr nie istnieje w tym modelu albo zapytanie obejmuje dziurę w mapie</td><td>sprawdź adres (0-based?) i wersję licznika; aplikacja sama dzieli odrzucone bloki na mniejsze</td></tr>
    <tr><td>Wyjątek 03 (niedozwolona wartość)</td><td>za dużo rejestrów w jednym zapytaniu</td><td>zmniejsz <code>"read": {"max_block": 32}</code> w presecie</td></tr>
    <tr><td>Wyjątek 0A / 0B</td><td>bramka nie dostała odpowiedzi od licznika po RS-485</td><td>sprawdź ustawienia portu szeregowego w bramce i Unit ID</td></tr>
    <tr><td>Wartości typu 1e-38, 2e-41, 1e+23, NaN</td><td>zła kolejność bajtów albo adres przesunięty o 1</td><td>porównaj kolumny ABCD/CDAB/BADC/DCBA w skanerze; ustaw <code>byte_order</code></td></tr>
    <tr><td>Wartości 10×, 100× lub 1000× za duże</td><td>liczba całkowita bez skali (np. 0,1 V albo Wh zamiast kWh)</td><td>ustaw <code>"scale": 0.1</code>, <code>0.01</code> lub <code>0.001</code></td></tr>
    <tr><td>Napięcie 0 albo dziwne liczby, reszta OK</td><td>rejestr to <code>uint16</code>/<code>int32</code>, a czytany jest jako <code>float32</code></td><td>ustaw <code>"type"</code> rejestru (skaner pokazuje kolumny int)</td></tr>
    <tr><td>Wartości stale 0</td><td>brak obciążenia albo zły rejestr</td><td>włącz odbiornik (czajnik) i obserwuj tryb Live</td></tr>
    <tr><td>Losowe błędy CRC, co któryś odczyt nieudany</td><td>zakłócenia, brak terminacji, za długi kabel, mini-UART</td><td>terminacja 120 Ω, skrętka, GND, niższa prędkość; zwiększ przerwę między ramkami</td></tr>
    <tr><td>Urządzenie "Nieaktualne"</td><td>licznik odpowiada wolniej niż interwał odczytu</td><td>zwiększ interwał, timeout i przerwę między ramkami (<code>--timeout</code>, <code>--delay-ms</code>)</td></tr>
    <tr><td>Błędy tylko przy kilku licznikach</td><td>dwa liczniki z tym samym Unit ID</td><td>podłącz każdy osobno i nadaj mu inny adres</td></tr>
    <tr><td>MQTT: brak połączenia</td><td>broker nie działa, zły port, hasło albo TLS</td><td>sprawdź <i>Ostatni błąd</i> w Integracjach; przetestuj <code>mosquitto_sub</code> z tymi samymi danymi</td></tr>
  </tbody>
</table></div>`;

const CLI_ROWS = [
  ['Serwer WWW'],
  ['--host', '0.0.0.0', 'adres nasłuchu HTTP (127.0.0.1 = tylko lokalnie)'],
  ['--port', '5000', 'port HTTP panelu'],
  ['--auth USER:HASŁO', 'brak', 'logowanie HTTP Basic (lub zmienna MODBUS_DASH_AUTH)'],
  ['--allow-write', 'wyłączone', 'zezwala na zapis rejestrów i cewek z interfejsu'],
  ['--data-dir', './data', 'katalog na konfigurację (config.json) i historię (history.sqlite)'],
  ['--presets-dir', './presets', 'katalog presetów użytkownika'],
  ['--no-history', 'wyłączone', 'nie zapisuje historii w SQLite (zostaje bufor w pamięci)'],
  ['--debug', 'wyłączone', 'tryb debug Flask; wymusza nasłuch na 127.0.0.1'],
  ['--log-level', 'INFO', 'DEBUG, INFO, WARNING albo ERROR'],
  ['Połączenie Modbus (magistrala default)'],
  ['--serial PORT', 'brak', 'port RS-485, np. /dev/serial0, /dev/ttyUSB0, COM3'],
  ['--baudrate', '9600', 'prędkość portu szeregowego'],
  ['--parity', 'N', 'parzystość: N, E albo O'],
  ['--stopbits', '1', 'bity stopu: 1 albo 2'],
  ['--bytesize', '8', 'bity danych: 7 albo 8'],
  ['--framer', 'rtu', 'ramkowanie na porcie szeregowym: rtu albo ascii'],
  ['--local-echo', 'wyłączone', 'adapter RS-485 odsyła własną transmisję (lokalne echo) - odrzucaj ją'],
  ['--tcp HOST[:PORT]', 'brak', 'licznik albo bramka Modbus TCP (port domyślnie 502)'],
  ['--rtu-over-tcp HOST[:PORT]', 'brak', 'bramka transparentna (ramki RTU po TCP), np. USR-TCP232, Elfin EW11'],
  ['--timeout', '1.0', 'timeout odpowiedzi [s]'],
  ['--retries', '1', 'liczba ponowień przy braku odpowiedzi'],
  ['--delay-ms', '0', 'dodatkowa przerwa między ramkami [ms]'],
  ['--preset', 'brak', 'od razu odczytuj urządzenie wg presetu (id), np. eastron_sdm630'],
  ['--unit', '1', 'Unit ID urządzenia dla --preset'],
  ['--interval', '1.0', 'interwał odczytu dla --preset [s]'],
  ['Symulator'],
  ['--no-sim', 'wyłączone', 'nie uruchamiaj symulatora'],
  ['--sim', 'wyłączone', 'uruchom symulator także przy --serial, --tcp lub --rtu-over-tcp'],
  ['--modbus-port', '5020', 'port TCP symulatora'],
  ['--sim-preset PRESET[:UNIT]', 'brak', 'dodatkowe urządzenie w symulatorze (można powtarzać; Unit ID domyślnie 2)'],
  ['--sim-framing', 'tcp', 'ramkowanie symulatora: tcp (Modbus TCP) albo rtu (RTU over TCP)'],
  ['--sim-strict', 'wyłączone', 'symulator zwraca wyjątek 02 dla niezmapowanych adresów (jak wiele liczników)'],
];

const CLI_NOTES = `
<p><code>--serial</code>, <code>--tcp</code> i <code>--rtu-over-tcp</code> wykluczają się nawzajem i wyłączają symulator
(chyba że dodasz <code>--sim</code>). Ustawienia z flag nadpisują magistralę <code>default</code> tylko w pamięci -
w zakładce Połączenia jest ona wtedy zablokowana, a <code>config.json</code> pozostaje bez zmian.</p>
<pre class="code"># Komputer z symulatorem (domyślnie):
python app.py

# Raspberry Pi + nakładka RS-485, Eastron SDM630 o adresie 1:
python app.py --serial /dev/serial0 --baudrate 9600 --preset eastron_sdm630 --unit 1

# Orno OR-WE-517 na przejściówce USB (9600 8E1):
python app.py --serial /dev/ttyUSB0 --parity E --preset orno_or_we_517

# Bramka Modbus TCP albo bramka transparentna RTU over TCP:
python app.py --tcp 192.168.1.50:502
python app.py --rtu-over-tcp 192.168.1.60:8899

# Hasło do panelu, inny port, tylko lokalnie:
python app.py --auth admin:tajne-haslo --port 8080
python app.py --host 127.0.0.1

# Symulator udający dwa dodatkowe liczniki z biblioteki:
python app.py --sim-preset eastron_sdm120:2 --sim-preset orno_or_we_517:3 --sim-strict</pre>`;

const SECTIONS = [
  { id: 'start', title: 'Szybki start', html: START },
  { id: 'rs485', title: 'Podłączenie RS-485', html: RS485 },
  { id: 'uart', title: 'Raspberry Pi UART', html: UART },
  { id: 'params', title: 'Parametry połączenia', html: PARAMS },
  { id: 'functions', title: 'Typy rejestrów i kody funkcji', html: FUNCTIONS_HTML },
  { id: 'types', title: 'Typy danych i kolejność bajtów', html: TYPES },
  { id: 'addresses', title: 'Adresy', html: ADDRESSES },
  { id: 'scanner', title: 'Skaner - jak czytać wyniki', html: SCANNER },
  { id: 'detect', title: 'Rozpoznawanie licznika i szukanie Unit ID', html: DETECT },
  { id: 'integrations', title: 'Historia, MQTT / Home Assistant, Prometheus', html: INTEGRATIONS },
  { id: 'security', title: 'Bezpieczeństwo', html: SECURITY },
  { id: 'troubleshooting', title: 'Rozwiązywanie problemów', html: TROUBLE },
  { id: 'cli', title: 'Argumenty CLI', build: cliTable },
  { id: 'meters', title: 'Obsługiwane liczniki', build: null },   // budowane w mount (dane z API)
];

// ── pomocnicze ───────────────────────────────────────────────

const PL = { ą: 'a', ć: 'c', ę: 'e', ł: 'l', ń: 'n', ó: 'o', ś: 's', ź: 'z', ż: 'z' };
const norm = (s) => String(s ?? '').toLowerCase().replace(/[ąćęłńóśźż]/g, (c) => PL[c])
  .normalize('NFD').replace(/\p{Mn}/gu, '');

const REG_TYPES = { input: 'Input (FC04)', holding: 'Holding (FC03)' };

function serialText(s) {
  if (!s || typeof s !== 'object' || !s.baudrate) return null;
  const parity = String(s.parity || 'N').toUpperCase().slice(0, 1);
  return `${s.baudrate} ${s.bytesize || 8}${parity}${s.stopbits || 1}`;
}

function cliTable() {
  return [
    h('div', { class: 'table-wrap' }, h('table', { class: 'tbl help-cli' },
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Flaga'), h('th', { scope: 'col' }, 'Domyślnie'), h('th', { scope: 'col' }, 'Opis'))),
      h('tbody', null, CLI_ROWS.map((r) => (r.length === 1
        ? h('tr', { class: 'help-group' }, h('th', { colspan: '3', scope: 'colgroup' }, r[0]))
        : h('tr', null, h('td', { class: 'nowrap' }, h('code', null, r[0])), h('td', { class: 'nowrap' }, r[1]), h('td', null, r[2]))))))),
    h('div', { html: CLI_NOTES }),
  ];
}

// ── widok ────────────────────────────────────────────────────

export function mount(root, ctx) {
  const ac = new AbortController();
  const alive = () => !ac.signal.aborted;
  const sections = new Map();     // id -> <section>
  const tocLinks = new Map();     // id -> <button>
  const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const meters = metersSection(ctx, ac.signal, alive);

  const body = h('article', { class: 'prose help-body' }, SECTIONS.map((s) => {
    const titleId = `help-${s.id}-t`;
    const content = s.id === 'meters' ? meters.el : s.build ? s.build() : h('div', { html: s.html });
    const sec = h('section', { class: 'help-sec', id: `help-${s.id}`, 'aria-labelledby': titleId },
      h('h3', { id: titleId, tabindex: '-1' }, s.title), content);
    sections.set(s.id, sec);
    return sec;
  }));

  const toc = h('nav', { class: 'help-toc card', 'aria-labelledby': 'help-toc-t' },
    h('h3', { class: 'card-title', id: 'help-toc-t' }, 'Spis treści'),
    h('ol', null, SECTIONS.map((s) => {
      const b = h('button', { type: 'button', class: 'help-toc-link', onclick: () => go(s.id, true) }, s.title);
      tocLinks.set(s.id, b);
      return h('li', null, b);
    })));

  const topBtn = h('button', {
    type: 'button', class: 'btn btn-ghost btn-sm help-top', hidden: true,
    onclick: () => {
      toc.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
      const first = toc.querySelector('button');
      if (first) first.focus({ preventScroll: true });
    },
  }, '↑ Spis treści');

  fill(root, h('div', { class: 'v-help' },
    pageHeader('Pomoc'),
    h('div', { class: 'page-content' },
      h('div', { class: 'help-layout' }, toc, body)),
    topBtn));

  function setCurrent(id) {
    tocLinks.forEach((b, k) => {
      if (k === id) b.setAttribute('aria-current', 'true');
      else b.removeAttribute('aria-current');
    });
  }

  function go(id, smooth) {
    const sec = sections.get(id);
    if (!sec) return;
    sec.scrollIntoView({ behavior: smooth && !reduceMotion ? 'smooth' : 'instant', block: 'start' });
    const heading = sec.querySelector('h3');
    if (heading) heading.focus({ preventScroll: true });
    setCurrent(id);
    // adres z sekcją (np. do wysłania komuś); replaceState nie wywołuje hashchange, więc widok się nie przeładuje
    try {
      history.replaceState(history.state, '', `#help?s=${encodeURIComponent(id)}`);
    } catch (e) {
      console.info('Nie udało się zaktualizować adresu:', e && e.message);
    }
  }

  // podświetlenie bieżącej sekcji w spisie treści i przycisk powrotu (na wąskich ekranach)
  const secList = [...sections.entries()];
  function syncScroll() {
    topBtn.hidden = window.scrollY < 700;
    let current = secList[0][0];
    for (const [id, sec] of secList) {
      if (sec.getBoundingClientRect().top <= 120) current = id;
      else break;
    }
    setCurrent(current);
  }
  let raf = 0;
  const onScroll = () => {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; syncScroll(); });
  };
  window.addEventListener('scroll', onScroll, { passive: true });

  meters.load();

  // #help?s=uart - przewinięcie do sekcji po wejściu (np. z linku w innym widoku)
  const wanted = ctx.params && ctx.params.s;
  if (wanted && sections.has(wanted)) requestAnimationFrame(() => { if (alive()) go(wanted, false); });
  else {
    window.scrollTo({ top: 0, behavior: 'instant' });   // router nie przewija przy zmianie widoku
    syncScroll();
  }

  return {
    unmount() {
      ac.abort();
      window.removeEventListener('scroll', onScroll);
      if (raf) cancelAnimationFrame(raf);
      meters.stop();
    },
  };
}

// ── obsługiwane liczniki ─────────────────────────────────────

function metersSection(ctx, signal, alive) {
  let all = null;
  let timer = 0;

  const search = h('input', {
    type: 'search', id: 'help-meter-q', autocomplete: 'off', spellcheck: 'false',
    placeholder: 'np. Eastron, SDM630, 3F, holding, 8E1', disabled: true,
    oninput: () => { clearTimeout(timer); timer = setTimeout(render, 120); },
    onkeydown: (e) => { if (e.key === 'Escape' && search.value) { e.stopPropagation(); search.value = ''; render(); } },
  });
  const count = h('span', { class: 'small muted', 'aria-live': 'polite' });
  const tableBox = h('div', null, h('div', { class: 'status-line' }, h('span', { class: 'spinner' }), 'Wczytywanie biblioteki presetów...'));

  const el = h('div', { class: 'help-meters' },
    h('p', null, 'Liczniki z wbudowanej biblioteki presetów (', h('code', null, 'presets/library'),
      '). Ustawienia portu to wartości fabryczne z dokumentacji - licznik mógł zostać przestawiony, a puste pole oznacza, że trzeba je sprawdzić w menu licznika. Przycisk ',
      h('i', null, 'Dodaj'), ' otwiera formularz nowego urządzenia z wybranym presetem.'),
    h('div', { class: 'toolbar help-meter-bar' },
      h('div', { class: 'field' }, h('label', { for: search.id }, 'Szukaj licznika'), search),
      count),
    tableBox,
    h('p', { class: 'small muted' }, 'Twojego licznika nie ma na liście? Zeskanuj go w ', h('a', { href: '#scanner' }, 'Skanerze rejestrów'),
      ' i utwórz własny preset - wiele liczników innych marek ma też mapę zgodną z jednym z modeli powyżej.'));

  function haystack(p) {
    const serial = serialText(p.serial) || '';
    return norm([p.manufacturer, p.model, p.name, p.id, p.description, p.phases ? `${p.phases}f ${p.phases}-faz` : '',
      p.register_type, p.register_type === 'holding' ? 'fc03' : 'fc04', serial].join(' '));
  }

  async function load() {
    try {
      const list = await get('/api/presets', { signal });
      if (!alive()) return;
      all = (Array.isArray(list) ? list : [])
        .filter((p) => p.builtin && !String(p.id).startsWith('simulator'))
        .map((p) => ({ ...p, _hay: haystack(p) }))
        .sort((a, b) => String(a.manufacturer || '').localeCompare(String(b.manufacturer || ''), 'pl')
          || String(a.model || a.name || '').localeCompare(String(b.model || b.name || ''), 'pl'));
      search.disabled = false;
      render();
    } catch (e) {
      if (e.name === 'AbortError' || !alive()) return;
      const retry = h('button', {
        type: 'button', class: 'btn btn-ghost btn-sm',
        onclick: () => { fill(tableBox, h('div', { class: 'status-line' }, h('span', { class: 'spinner' }), 'Wczytywanie...')); load(); },
      }, 'Spróbuj ponownie');
      fill(tableBox, h('div', { class: 'notice notice-err' }, h('strong', null, 'Nie udało się pobrać listy presetów: '), e.message || String(e), ' ', retry));
    }
  }

  function render() {
    if (!all) return;
    const tokens = norm(search.value).split(/\s+/).filter(Boolean);
    const rows = tokens.length ? all.filter((p) => tokens.every((t) => p._hay.includes(t))) : all;
    count.textContent = tokens.length ? `Pokazano ${rows.length} z ${all.length}` : `${all.length} modeli`;
    if (!rows.length) {
      fill(tableBox, h('div', { class: 'empty help-empty' },
        h('p', null, all.length ? `Brak liczników pasujących do „${search.value.trim()}”.` : 'Biblioteka presetów jest pusta.'),
        all.length ? h('div', { class: 'actions' }, h('button', {
          type: 'button', class: 'btn btn-ghost btn-sm',
          onclick: () => { search.value = ''; render(); search.focus(); },
        }, 'Wyczyść filtr')) : null));
      return;
    }
    fill(tableBox, h('div', { class: 'table-wrap tall' }, h('table', { class: 'tbl help-meter-tbl' },
      h('thead', null, h('tr', null,
        h('th', { scope: 'col' }, 'Producent'), h('th', { scope: 'col' }, 'Model i preset'), h('th', { scope: 'col' }, 'Fazy'),
        h('th', { scope: 'col' }, 'Rejestry'), h('th', { scope: 'col' }, 'Port (fabr.)'),
        h('th', { scope: 'col', class: 'right', title: 'liczba wartości w presecie' }, 'Wartości'),
        h('th', { scope: 'col' }, h('span', { class: 'sr-only' }, 'Akcje')))),
      h('tbody', null, rows.map(row)))));
  }

  function row(p) {
    const serial = serialText(p.serial);
    const model = p.model || p.name || p.id;
    return h('tr', null,
      h('td', null, p.manufacturer || '-'),
      h('td', { class: 'help-model' },
        h('div', { class: 'help-model-name' }, model,
          p.valid === false ? h('span', { class: 'badge badge-err', title: (p.errors || []).join('; ') }, 'błąd presetu') : null),
        h('code', { class: 'help-preset-id' }, p.id),
        p.description ? h('details', null, h('summary', null, 'opis'), h('p', null, p.description)) : null),
      h('td', { class: 'nowrap' }, p.phases ? `${p.phases}F` : '-'),
      h('td', { class: 'nowrap' }, REG_TYPES[p.register_type] || p.register_type || '-'),
      h('td', { class: 'nowrap mono' }, serial || h('span', { class: 'muted' }, '-')),
      h('td', { class: 'num' }, p.register_count ?? '-'),
      h('td', { class: 'right' },
        h('button', {
          type: 'button', class: 'btn btn-ghost btn-sm', 'aria-label': `Dodaj urządzenie z presetem ${p.name || model}`,
          onclick: () => ctx.navigate('devices', { new: '1', preset: p.id }),
        }, 'Dodaj')));
  }

  return { el, load, stop: () => clearTimeout(timer) };
}
