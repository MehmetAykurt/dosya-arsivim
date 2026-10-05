# Dosya Arşivim

## Önemli duyuru: Geliştirme sonlandırılmıştır

Dosya Arşivim eklentisinin geliştirilmesi, 25 Eylül 2026 tarihi itibarıyla sonlandırılmıştır.

Eklentinin çalışması için kullanılan sunucu altyapısının kararlı ve güvenilir bir çalışma deneyimi sunamaması nedeniyle projenin sürdürülebilirliği kalmamıştır. Bu nedenle bundan sonra tarafımdan yeni sürüm yayımlanmayacak; hata düzeltmesi, yeni özellik ve güncel NVDA sürümleri için uyumluluk çalışması yapılmayacaktır.

Mevcut sürüm kullanılmaya devam edilebilir. Ancak eklentinin bağlı olduğu sunucu hizmetindeki sorunlar nedeniyle eklentinin bütün işlevlerinin düzgün ve kesintisiz çalışacağı garanti edilmemektedir. Kullanıcıların önemli dosyalarının tek kopyasını bu hizmette tutmamaları ve gerekli yedekleri kendi bilgisayarlarında veya güvenilir başka bir ortamda saklamaları önerilir.

Projenin kaynak kodu ve yayımlanmış önceki sürümleri, inceleme ve arşiv amacıyla erişime açık tutulacaktır.

### Geliştirmeye devam etmek isteyenlere

Dosya Arşivim'i geliştirmeyi sürdürmek isteyen geliştiriciler, GitHub'daki Fork (çatallama) özelliğiyle bu deponun kendi hesaplarında bir kopyasını oluşturabilir; kaynak kodunu inceleyebilir, hataları giderebilir, yeni özellikler ekleyebilir ve geliştirdikleri sürümleri yayımlayabilirler. Projede yer alan dosyalar, mevcut GNU Genel Kamu Lisansı sürüm 2 (GPL v2) koşulları çerçevesinde bu amaçlarla kullanılabilir.

Bu çalışmalarda özgün geliştirici Mehmet Aykurt'a ait telif bildirimleri, mevcut lisans metni ve diğer telif bildirimleri korunmalı; yapılan değişiklikler ve değişiklik tarihleri açıkça belirtilmelidir. Dağıtılan türev çalışmaların kaynak kodu da GPL v2 koşullarına uygun biçimde sunulmalıdır. Projenin başlangıç noktasının anlaşılabilmesi için bu depoya bağlantı verilmesini rica ederim.

Kendi kopyanızda sürdüreceğiniz geliştirme ve yayımlayacağınız sürümlerin bakımını siz üstlenebilirsiniz. Benim bu depo üzerinden geliştirme ve destek çalışmalarımı sonlandırmış olmam, başkalarının projeyi sürdürmesine engel değildir.

Bu çalışmanın NVDA kullanıcılarına daha kararlı ve yararlı bir eklenti olarak ulaşmasına katkıda bulunmak isteyen herkese şimdiden teşekkür ederim.

Anlayışınız için teşekkür ederim.

**Dosya Arşivim**, NVDA ekran okuyucusu kullanıcılarının dosyalarını erişilebilir biçimde saklamasını, düzenlemesini, indirmesini ve paylaşmasını sağlayan bir eklentidir.

Eklenti, hesap işlemlerini e-posta ile gönderilen tek kullanımlık kodla yürütür. Parola eklenti içinde saklanmaz. Dosya listeleri yerel SQLite önbelleğinde tutulur ve sunucuyla arka planda eşitlenir.

## Sürüm bilgileri

- **Sürüm:** 26.8.18
- **En düşük NVDA sürümü:** 2026.1
- **Son test edilen NVDA sürümü:** 2026.1.1
- **Geliştirici:** Mehmet Aykurt
- **Lisans:** GNU Genel Kamu Lisansı, sürüm 2.0

## Öne çıkan özellikler

- `NVDA+Alt+A` ile Dosya Arşivim penceresini açma
- E-posta ile tek kullanımlık kod göndererek giriş yapma veya yeni hesap oluşturma
- Hesap bilgisini yalnızca bu bilgisayardan kaldırma ve gerektiğinde yeniden bağlanma
- Resimler, Ses ve Müzik, Videolar, Belgeler, Uygulamalar, Sıkıştırılmış Dosyalar, Yedekler ve Diğer klasörleri
- Dosya seçerek veya panodan dosya yollarını kullanarak yükleme
- Kalıcı yükleme kuyruğu, işlem durumu ve hatalı yüklemeleri yeniden deneme
- Dosya indirme, doğrudan bağlantıyı panoya kopyalama ve dosya bilgilerini görüntüleme
- Dosyayı silme
- Yalnızca özgün dosyaları veya isteğe bağlı olarak sunucunun oluşturduğu türetilmiş dosyaları görüntüleme
- Yerel SQLite önbelleğiyle hızlı listeleme ve arka plan eşitlemesi
- Yükleme bildirimleri ile türetilmiş dosyaların görünürlüğünü ayarlama
- Türkçe ve İngilizce arayüz ile kullanım kılavuzu

## Belgeler

Ayrıntılı Türkçe kullanım kılavuzu [`doc/tr/readme.html`](doc/tr/readme.html), İngilizce kılavuz ise [`doc/en/readme.html`](doc/en/readme.html) dosyasındadır.

Dosya Arşivim bağımsız bir NVDA eklentisidir. Dosyaların saklanması, paylaşılması ve telif haklarına ilişkin sorumluluk kullanıcıya aittir.
