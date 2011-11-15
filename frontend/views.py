import logging
from decimal import Decimal as D

from django.db.models import Q
from django.conf import settings
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.core.exceptions import ObjectDoesNotExist
from django.forms import Select
from django.http import HttpResponseRedirect
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic.edit import FormView
from django.views.generic.base import TemplateView
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, render_to_response, get_object_or_404

import oauth2 as oauth
from itertools import islice

from regluit.core import tasks
from regluit.core import models, bookloader
from regluit.core.search import gluejar_search
from regluit.core.goodreads import GoodreadsClient
from regluit.frontend.forms import UserData, ProfileForm, CampaignPledgeForm, GoodreadsShelfLoadingForm
from regluit.payment.manager import PaymentManager
from regluit.payment.parameters import TARGET_TYPE_CAMPAIGN

from regluit.core import goodreads
from tastypie.models import ApiKey

logger = logging.getLogger(__name__)

from regluit.payment.models import Transaction

import urllib
from re import sub

def home(request):
    if request.user.is_authenticated():
        return HttpResponseRedirect(reverse('supporter',
            args=[request.user.username]))
    return render(request, 'home.html', {'suppress_search_box': True})

def stub(request):
    path = request.path[6:] # get rid of /stub/
    return render(request,'stub.html', {'path': path})

def work(request, work_id, action='display'):
    work = get_object_or_404(models.Work, id=work_id)
    campaign = work.last_campaign()
    if campaign:
        q = Q(campaign=campaign) | Q(campaign__isnull=True)
        premiums = models.Premium.objects.filter(q)
    else:
        premiums = None
    if action == 'setup_campaign':
        return render(request, 'setup_campaign.html', {'work': work})
    else:
        return render(request, 'work.html', {'work': work, 'premiums': premiums})
        
def workstub(request, title, imagebase, image, author, googlebooks_id, action='display'):
	premiums = None
	title = urllib.unquote(title)
	imagebase = urllib.unquote(imagebase)
	image = urllib.unquote(image)
	author = urllib.unquote(author)
	return render(request, 'workstub.html', {'title': title, 'image': image, 'imagebase': imagebase, 'author': author, 'googlebooks_id': googlebooks_id, 'premiums': premiums})


def pledge(request,work_id):
    work = get_object_or_404(models.Work, id=work_id)
    campaign = work.last_campaign()
    if campaign:
        premiums = campaign.premiums.all()
        if premiums.count() == 0:
            premiums = models.Premium.objects.filter(campaign__isnull=True)
    premium_id = request.GET.get('premium_id', None)
    if premium_id is not None:
        preapproval_amount = D(models.Premium.objects.get(id=premium_id).amount)
    else:
        preapproval_amount = D('0.00')
    data = {'preapproval_amount':preapproval_amount}
    form = CampaignPledgeForm(data)

    return render(request,'pledge.html',{'work':work,'campaign':campaign, 'premiums':premiums, 'form':form})

def supporter(request, supporter_username, template_name):
    supporter = get_object_or_404(User, username=supporter_username)
    wishlist = supporter.wishlist
    backed = 0
    backing = 0
    transet = Transaction.objects.all().filter(user = supporter)
    
    for transaction in transet:
        try:
            if(transaction.campaign.status == 'SUCCESSFUL'):
                backed += 1
            elif(transaction.campaign.status == 'ACTIVE'):
                backing += 1
        except:
            continue
            
    wished = supporter.wishlist.works.count()
    
    date = supporter.date_joined.strftime("%B %d, %Y")

    # figure out what works the users have in commmon if someone
    # is looking at someone else's supporter page
    if not request.user.is_anonymous and request.user != supporter:
        w1 = request.user.wishlist
        w2 = supporter.wishlist
        shared_works = models.Work.objects.filter(wishlists__in=[w1])
        shared_works = list(shared_works.filter(wishlists__in=[w2]))
    else: 
        shared_works = []

    # following block to support profile admin form in supporter page
    if request.user.is_authenticated() and request.user.username == supporter_username:
        try:
            profile_obj=request.user.get_profile()
        except ObjectDoesNotExist:
            profile_obj= models.UserProfile()
            profile_obj.user=request.user
        if  request.method == 'POST': 
            profile_form = ProfileForm(data=request.POST,instance=profile_obj)
            if profile_form.is_valid():
                if profile_form.cleaned_data['clear_facebook'] or profile_form.cleaned_data['clear_twitter'] :
                    if profile_form.cleaned_data['clear_facebook']:
                        profile_obj.facebook_id=0
                    if profile_form.cleaned_data['clear_twitter']:
                        profile_obj.twitter_id=""
                    profile_obj.save()
                profile_form.save()
        else:
            profile_form= ProfileForm(instance=profile_obj)
    else:
        profile_form = ''
            
    context = {
            "supporter": supporter,
            "wishlist": wishlist,
            "backed": backed,
            "backing": backing,
            "wished": wished,
            "date": date,
            "shared_works": shared_works,
            "profile_form": profile_form,
    }
    
    return render(request, template_name, context)

def edit_user(request):
    form=UserData()
    if not request.user.is_authenticated():
        return HttpResponseRedirect(reverse('auth_login'))
    oldusername=request.user.username
    if request.method == 'POST': 
        # surely there's a better way to add data to the POST data?
        postcopy=request.POST.copy()
        postcopy['oldusername']=oldusername 
        form = UserData(postcopy)
        if form.is_valid(): # All validation rules pass, go and change the username
            request.user.username=form.cleaned_data['username']
            request.user.save()
            return HttpResponseRedirect(reverse('home')) # Redirect after POST
    return render(request,'registration/user_change_form.html', {'form': form},)  


def search(request):
    q = request.GET.get('q', None)
    results = gluejar_search(q)

    # flag search result as on wishlist as appropriate
    if not request.user.is_anonymous():
        # get a list of all the googlebooks_ids for works on the user's wishlist
        wishlist = request.user.wishlist
        editions = models.Edition.objects.filter(work__wishlists__in=[wishlist])
        googlebooks_ids = [e['googlebooks_id'] for e in editions.values('googlebooks_id')]

        # if the results is on their wishlist flag it
        for result in results:
            if result['googlebooks_id'] in googlebooks_ids:
                result['on_wishlist'] = True
            else:
                result['on_wishlist'] = False
            
    # also urlencode some parameters we'll need to pass to workstub in the title links
    # needs to be done outside the if condition
    for result in results:
    	result['urlimage'] = urllib.quote(sub('^https?:\/\/','', result['image']).encode("utf-8"), safe='')
    	result['urlauthor'] = urllib.quote(result['author'].encode("utf-8"), safe='')
    	result['urltitle'] = urllib.quote(result['title'].encode("utf-8"), safe='')

    context = {
        "q": q,
        "results": results,
    }
    return render(request, 'search.html', context)

# TODO: perhaps this functionality belongs in the API?
@require_POST
@login_required
@csrf_exempt
def wishlist(request):
    googlebooks_id = request.POST.get('googlebooks_id', None)
    remove_work_id = request.POST.get('remove_work_id', None)
    if googlebooks_id:
        edition = bookloader.add_by_googlebooks_id(googlebooks_id)
        # add related editions asynchronously
        tasks.add_related.delay(edition.isbn_10)
        request.user.wishlist.works.add(edition.work)
        # TODO: redirect to work page, when it exists
        return HttpResponseRedirect('/')
    elif remove_work_id:
        work = models.Work.objects.get(id=int(remove_work_id))
        request.user.wishlist.works.remove(work)
        # TODO: where to redirect?
        return HttpResponseRedirect('/')
  
class CampaignFormView(FormView):
    template_name="campaign_detail.html"
    form_class = CampaignPledgeForm
    
    def get_context_data(self, **kwargs):
        pk = self.kwargs["pk"]
        campaign = models.Campaign.objects.get(id=int(pk))
        context = super(CampaignFormView, self).get_context_data(**kwargs)
        context.update({
           'campaign': campaign
        })
        return context

    def form_valid(self,form):
        pk = self.kwargs["pk"]
        preapproval_amount = form.cleaned_data["preapproval_amount"]
        anonymous = form.cleaned_data["anonymous"]
        
        # right now, if there is a non-zero pledge amount, go with that.  otherwise, do the pre_approval
        campaign = models.Campaign.objects.get(id=int(pk))
        
        p = PaymentManager()
                    
        # we should force login at this point -- or if no account, account creation, login, and return to this spot
        if self.request.user.is_authenticated():
            user = self.request.user
        else:
            user = None
            
        # calculate the work corresponding to the campaign id
        work_id = campaign.work.id
        return_url = self.request.build_absolute_uri(reverse('work',kwargs={'work_id': str(work_id)}))
        t, url = p.authorize('USD', TARGET_TYPE_CAMPAIGN, preapproval_amount, campaign=campaign, list=None, user=user,
                            return_url=return_url, anonymous=anonymous)    
 
        #else:
        #    # instant payment:  send to the partnering RH
        #    # right now, all money going to Gluejar.  
        #    receiver_list = [{'email':settings.PAYPAL_GLUEJAR_EMAIL, 'amount':pledge_amount}]
        #    
        #    # redirect the page back to campaign page on success
        #    #return_url = self.request.build_absolute_uri("/campaigns/%s" %(str(pk)))
        #    return_url = self.request.build_absolute_uri(reverse('campaign_by_id',kwargs={'pk': str(pk)}))
        #    t, url = p.pledge('USD', TARGET_TYPE_CAMPAIGN, receiver_list, campaign=campaign, list=None, user=user,
        #                      return_url=return_url, anonymous=anonymous)
        
        if url:
            logger.info("CampaignFormView paypal: " + url)
            return HttpResponseRedirect(url)
        else:
            response = t.reference
            logger.info("CampaignFormView paypal: Error " + str(t.reference))
            return HttpResponse(response)


class GoodreadsDisplayView(TemplateView):
    template_name = "goodreads_display.html"
    def get_context_data(self, **kwargs):
        context = super(GoodreadsDisplayView, self).get_context_data(**kwargs)
        session = self.request.session
        gr_client = GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
        
        user = self.request.user
        if user.is_authenticated():
            api_key = ApiKey.objects.filter(user=user)[0].key
            context['api_key'] = api_key

        if user.profile.goodreads_user_id is None:   
            # calculate the Goodreads authorization URL
            (context["goodreads_auth_url"], request_token) = gr_client.begin_authorization(self.request.build_absolute_uri(reverse('goodreads_cb')))
            logger.info("goodreads_auth_url: %s" %(context["goodreads_auth_url"]))
            # store request token in session so that we can redeem it for auth_token if authorization works
            session['goodreads_request_token'] = request_token['oauth_token']
            session['goodreads_request_secret'] = request_token['oauth_token_secret']
        else:
            gr_shelves = gr_client.shelves_list(user_id=user.profile.goodreads_user_id)
            context["shelves_info"] = gr_shelves
            gr_shelf_load_form = GoodreadsShelfLoadingForm()
            # load the shelves into the form
            choices = [('all','all (%d)' % (gr_shelves["total_book_count"]))] + [(s["name"],"%s (%d)" % (s["name"],s["book_count"])) for s in gr_shelves["user_shelves"]]
            gr_shelf_load_form.fields['goodreads_shelf_name'].widget = Select(choices=tuple(choices))
            
            context["gr_shelf_load_form"] = gr_shelf_load_form
            #context["reviews"] = list(islice(gr_client.review_list(user_id=user.profile.goodreads_user_id, per_page=50),50))
  
        return context

@login_required    
def goodreads_cb(request):
    """handle callback from Goodreads"""
    
    session = request.session
    authorized_flag = request.GET['authorize']  # is it '1'?
    request_oauth_token = request.GET['oauth_token']

    if authorized_flag == '1':
        request_token = {'oauth_token': session.get('goodreads_request_token'),
                         'oauth_token_secret': session.get('goodreads_request_secret')}
        gr_client = GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
        
        access_token = gr_client.complete_authorization(request_token)
        
        # store the access token in the user profile
        profile = request.user.profile
        profile.goodreads_auth_token = access_token["oauth_token"]
        profile.goodreads_auth_secret = access_token["oauth_token_secret"]
    
        # let's get the userid, username
        user = gr_client.auth_user()
        
        profile.goodreads_user_id = user["userid"]
        profile.goodreads_user_name = user["name"]
        profile.goodreads_user_link = user["link"]
        
        profile.save()  # is this needed?

    # redirect to the Goodreads display page -- should observe some next later
    return HttpResponseRedirect(reverse('goodreads_display'))

@require_POST
@login_required
@csrf_exempt    
def goodreads_flush_assoc(request):
    user = request.user
    if user.is_authenticated():
        profile = user.profile
        profile.goodreads_user_id = None
        profile.goodreads_user_name = None
        profile.goodreads_user_link = None
        profile.goodreads_auth_token = None
        profile.goodreads_auth_secret = None
        profile.save()
    return HttpResponseRedirect(reverse('goodreads_display'))
      
@require_POST
@login_required      
@csrf_exempt
def goodreads_load_shelf(request):
    """
    a view to allow user load goodreads shelf into her wishlist
    """
    # Should be moved to the API
    goodreads_shelf_name = request.POST.get('goodreads_shelf_name', 'all')
    user = request.user
    try:
        logger.info('Adding task to load shelf %s to user %s', goodreads_shelf_name, user)
        tasks.load_goodreads_shelf_into_wishlist.delay(user, goodreads_shelf_name)
        return HttpResponse("Shelf loading placed on task queue.")
    except Exception,e:
        return HttpResponse("Error in loading shelf: %s " % (e))
        logger.info("Error in loading shelf: %s ", e)

@require_POST
@login_required      
@csrf_exempt
def clear_wishlist(request):
    try:
        request.user.wishlist.works.clear()
        return HttpResponse('wishlist cleared')
    except Exception, e:
        return HttpResponse("Error in clearing wishlist: %s " % (e))
        logger.info("Error in clearing wishlist: %s ", e)
    

